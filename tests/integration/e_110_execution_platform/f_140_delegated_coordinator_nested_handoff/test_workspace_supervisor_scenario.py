"""Workspace callback supervisor end-to-end scenario (Redmine #13683 Phase A).

Exercises the real cross-workspace fan-out over a **real** home workspace registry (two registered
workspaces), with injected roster / Redmine source / sender so the scenario is hermetic:

- one supervised sweep enumerates the registry and, per workspace, supplies durable events (so
  `workflow glance` stops reporting `unknown`) and drains that workspace's callback partition;
- a concurrent duplicate daemon (a second holder while the first still holds its leases) is fenced
  across the WHOLE registry — every workspace is skipped, zero delivery.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore, supervisor_lease_path
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DELIVERED,
    WorkflowRuntimeStore,
    workflow_runtime_store_path,
)
from mozyo_bridge.core.state.workspace_registry import register_workspace
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
    default_workspaces,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
)


def _payload(issue: str) -> dict:
    return {
        "issue": {"id": issue},
        "journals": [
            {"id": f"j{issue}", "notes": f"[mozyo:workflow-event:gate=review_request:conclusion=pending]"}
        ],
    }


class _RecordingSender:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, row) -> str:
        self.calls.append(row)
        return SEND_DELIVERED


class WorkspaceSupervisorScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        # Two real registered workspaces under the temp home.
        self.repo_a = self.home / "repoA"
        self.repo_b = self.home / "repoB"
        self.repo_a.mkdir()
        self.repo_b.mkdir()
        rec_a = register_workspace(self.repo_a, home=self.home).record
        rec_b = register_workspace(self.repo_b, home=self.home).record
        # Map each real workspace_id to a distinct active issue for the injected roster.
        self.issue_by_ws = {rec_a.workspace_id: "13683", rec_b.workspace_id: "13684"}
        self.store_path = workflow_runtime_store_path(self.home)
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.sender = _RecordingSender()

    def _supervisor(self, *, holder, release_after=True):
        def roster_fn(ws: SupervisedWorkspace):
            issue = self.issue_by_ws.get(ws.workspace_id)
            return ((issue,) if issue else ()), ""

        def source_fn(ws: SupervisedWorkspace):
            issue = self.issue_by_ws.get(ws.workspace_id)
            return MappingRedmineJournalSource(payload=_payload(issue)) if issue else None

        return WorkspaceCallbackSupervisor(
            holder=holder,
            lease_store=SupervisorLeaseStore(path=supervisor_lease_path(self.home)),
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: default_workspaces(home=self.home),
            roster_fn=roster_fn,
            redmine_source_fn=source_fn,
            sender_fn=lambda ws: self.sender,
            release_after=release_after,
            clock=lambda: "2026-07-13T00:00:00+00:00",
        )

    def test_sweep_supplies_events_and_drains_all_registered_workspaces(self) -> None:
        report = self._supervisor(holder="superX").run_once()
        self.assertEqual(len(report.workspaces), 2)
        self.assertEqual(report.workspaces_supervised, 2)
        self.assertGreaterEqual(report.events_supplied, 2)  # one gate per workspace's issue
        self.assertEqual(report.delivered, 2)
        # Both issues' events are persisted for glance/resume.
        persisted = {e.issue for e in self.store.read_events()}
        self.assertEqual(persisted, {"13683", "13684"})
        # Both callbacks delivered, each partitioned to its own workspace.
        delivered = self.outbox.read(states=[CALLBACK_DELIVERED])
        self.assertEqual(len(delivered), 2)
        self.assertEqual({d.workspace_id for d in delivered}, set(self.issue_by_ws))

    def test_concurrent_duplicate_daemon_is_fenced_across_whole_registry(self) -> None:
        # Supervisor A holds all leases (release_after=False, still running).
        self._supervisor(holder="superA", release_after=False).run_once()
        self.sender.calls.clear()
        # Supervisor B (different holder) runs while A holds the leases -> every workspace skipped.
        report_b = self._supervisor(holder="superB").run_once()
        self.assertEqual(report_b.workspaces_supervised, 0)
        self.assertEqual(report_b.workspaces_skipped, 2)
        self.assertEqual(self.sender.calls, [])  # zero duplicate delivery


class _RecordingTransport:
    """A fake Redmine note transport that records the gate note (so --emit-gate reports recorded)."""

    def post_issue_note(self, issue_id: str, notes: str) -> str:
        return f"https://redmine.example/issues/{issue_id}#note-1"


class SupervisorWakeProducerE2ETest(unittest.TestCase):
    """R1-F2 end-to-end: the canonical gate writer emits a local wake the supervisor consumes."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self._env = {}
        for key in ("MOZYO_BRIDGE_HOME", "MOZYO_WORKSPACE_ID"):
            self._env[key] = os.environ.get(key)
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)
        os.environ["MOZYO_WORKSPACE_ID"] = "wsA"

    def tearDown(self) -> None:
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_emit_gate_enqueues_wake_that_supervisor_consumes(self) -> None:
        from mozyo_bridge.application.cli import build_parser
        from mozyo_bridge.core.state.supervisor_wake import SupervisorWakeStore

        parser = build_parser()
        args = parser.parse_args(
            ["workflow", "callbacks", "--emit-gate", "--issue", "13683",
             "--gate", "review_request", "--target-head", "a" * 40, "--json"]
        )
        # Force the credential-gated transport to a recording fake so the gate RECORDS; stub the
        # activation to a no-op so this test observes the enqueue deterministically (the activation
        # itself is covered by the full-chain test).
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure."
            "redmine_note_transport.redmine_delivery_transport_from_env",
            return_value=_RecordingTransport(),
        ), mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.cli_workflow_callbacks._activate_supervisor_process",
            lambda: None,
        ):
            rc = args.func(args)
        self.assertEqual(rc, 0)  # gate recorded
        # The gate commit emitted a durable local wake for (wsA, 13683).
        pending = SupervisorWakeStore(home=self.home).pending()
        self.assertEqual([h.as_tuple() for h in pending], [("wsA", "13683")])

    def test_gate_emit_activates_supervisor_to_delivery_full_chain(self) -> None:
        # The full R3-F1 path: gate emit -> durable wake + ACTIVATION -> the activated supervisor
        # consumes the wake -> durable event appended + callback outbox delivered through the REAL
        # background_service authority. The test never calls run_once itself — the gate commit's
        # activation seam drives it.
        from mozyo_bridge.application.cli import build_parser
        from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
        from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore, supervisor_lease_path
        from mozyo_bridge.core.state.supervisor_wake import SupervisorWakeStore, supervisor_wake_path
        from mozyo_bridge.core.state.workflow_runtime_store import (
            CALLBACK_DELIVERED,
            WorkflowRuntimeStore,
            workflow_runtime_store_path,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
            BackgroundServiceCallbackSender,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
            HandoffDeliveryResult,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
            SupervisedWorkspace,
            WorkspaceCallbackSupervisor,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.background_service_delivery import (
            DeliveryTarget,
            TargetResolution,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
            RoleProviderBinding,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
            SUPERVISION_LOCAL_WAKE,
        )

        store_path = workflow_runtime_store_path(self.home)
        store = WorkflowRuntimeStore(path=store_path)
        outbox = CallbackOutbox(path=store_path)
        lease_store = SupervisorLeaseStore(path=supervisor_lease_path(self.home))
        transports = {}

        class _FakeResolver:
            def __init__(self, wsid):
                self._wsid = wsid

            def resolve(self, row):
                # A live coordinator (codex) target matching the row's durable expected tuple. The
                # generation comes from the LIVE authority — Phase A has none (#13684 unconnected), so
                # it is blank, NEVER copied from the row. That blank live generation is what fences
                # delivery closed below (R6-F1).
                return TargetResolution.of([
                    DeliveryTarget(workspace_id=self._wsid, lane="default", receiver="codex",
                                   issue=str(row.issue), journal=str(row.journal),
                                   generation="", locator="%coord")
                ])

        class _CapturingTransport:
            def __init__(self):
                self.calls = []

            def deliver(self, row, target):
                self.calls.append((row, target))
                return HandoffDeliveryResult("sent", "ok")

        def sender_fn(ws):
            t = _CapturingTransport()
            transports[ws.workspace_id] = t
            return BackgroundServiceCallbackSender(
                workspace_id=ws.workspace_id, holder="superX", lease_store=lease_store,
                target_resolver=_FakeResolver(ws.workspace_id), transport=t, outbox=outbox,
                now_fn=lambda: "2026-07-13T00:00:00+00:00",
            )

        def _activate():
            # The activation seam (production spawns a detached run-once): here it runs an in-process
            # supervisor over the same home, consuming the just-enqueued wake and delivering.
            WorkspaceCallbackSupervisor(
                holder="superX", lease_store=lease_store, store=store, outbox=outbox,
                workspaces_fn=lambda: [SupervisedWorkspace("wsA", str(self.home / "repoA"))],
                roster_fn=lambda ws: (("13683",), ""),
                redmine_source_fn=lambda ws: MappingRedmineJournalSource(payload=_payload("13683")),
                sender_fn=sender_fn,
                binding_fn=lambda ws: RoleProviderBinding.default(),  # coordinator -> codex
                wake_store=SupervisorWakeStore(path=supervisor_wake_path(self.home)),
                clock=lambda: "2026-07-13T00:00:00+00:00",
            ).run_once(mode=SUPERVISION_LOCAL_WAKE)

        parser = build_parser()
        args = parser.parse_args(
            ["workflow", "callbacks", "--emit-gate", "--issue", "13683",
             "--gate", "review_request", "--target-head", "a" * 40, "--json"]
        )
        # gate emit -> records the gate -> enqueues the wake -> ACTIVATES the supervisor (the seam
        # is patched to run in-process; the test does not call run_once).
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure."
            "redmine_note_transport.redmine_delivery_transport_from_env",
            return_value=_RecordingTransport(),
        ), mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.cli_workflow_callbacks._activate_supervisor_process",
            _activate,
        ):
            self.assertEqual(args.func(args), 0)

        # The gate commit's activation drove the whole chain: durable wake -> activation -> event
        # supply. Delivery itself is fail-closed in Phase A because there is no live generation
        # authority (#13684 unconnected): the background_service transport is NEVER called and no row
        # reaches DELIVERED — a deterministic zero-send, not a silent uncorrelated push (R6-F1).
        self.assertEqual(transports.get("wsA", _CapturingTransport()).calls, [])  # generation-disabled
        self.assertEqual(len(outbox.read(states=[CALLBACK_DELIVERED])), 0)
        # The durable-event supply (the supervisor's primary function — 15/15 unknown resolution) is
        # unaffected by the delivery fence, and the wake is still consumed.
        self.assertEqual([e.issue for e in store.read_events()], ["13683"])
        self.assertEqual(SupervisorWakeStore(home=self.home).pending(), ())  # wake consumed


if __name__ == "__main__":
    unittest.main()
