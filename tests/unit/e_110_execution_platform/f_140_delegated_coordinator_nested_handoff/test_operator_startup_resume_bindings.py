"""Unit tests for the resume leg's live binding ports (Redmine #13813 F2, j#79332).

Hermetic tests for the structured send port, the credentialed gate-recorder port, and the
action-time target resolver — each exercised through its injectable sub-seams (runner /
transport / credentials / lifecycle / inventory / attestation / capture) so the live
composition is proven without any live tmux / Redmine / handoff rail.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DISPOSITION_ACTIVE,
    ProcessGenerationPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (  # noqa: E402
    TURN_START_NOT_STARTED,
    TURN_START_STARTED,
    TURN_START_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_record import (  # noqa: E402
    ResumeGateRecorder,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_send import (  # noqa: E402
    ResumeHandoffSendPort,
    map_handoff_stdout_to_send_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_target import (  # noqa: E402
    ResumeTargetResolver,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    AGENT_KEY_NAME,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (  # noqa: E402
    GateApproval,
    GateClassification,
    GateTarget,
    OriginalRequest,
    build_required_gate,
    repo_identity_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate_lattice import (  # noqa: E402
    approve_gate,
    report_operator_done,
)


def _target(**overrides) -> GateTarget:
    kwargs = dict(
        workspace_id="ws-alpha",
        repo_identity_digest=repo_identity_digest("repo-alpha"),
        execution_root=".",
        lane_id="lane-alpha",
        target_role="implementation_worker",
        target_assigned_name="worker-a",
        provider_id="claude",
        agent_generation=3,
        lane_revision=1,
    )
    kwargs.update(overrides)
    return GateTarget(**kwargs)


def _done_gate():
    return report_operator_done(
        approve_gate(
            build_required_gate(
                gate_id="gate-1",
                action_generation=1,
                original_request=OriginalRequest(
                    source="redmine", issue="13760", journal="77948", delivery_id="deliv-1"
                ),
                target=_target(),
                classification=GateClassification(
                    blocker_id="first_run_theme",
                    profile_version="2",
                    classifier_version="1",
                    observed_at="x",
                ),
            ),
            approval=GateApproval(source_journal="78412"),
        )
    )


class SendOutcomeMappingTests(unittest.TestCase):
    def test_verified_sent_ok_is_started(self) -> None:
        r = map_handoff_stdout_to_send_outcome(0, '{"status": "sent", "reason": "ok"}')
        self.assertEqual(r.turn_start, TURN_START_STARTED)

    def test_turn_start_started_is_started(self) -> None:
        r = map_handoff_stdout_to_send_outcome(0, '{"outcome": "started", "snapshot_state": "idle"}')
        self.assertEqual(r.turn_start, TURN_START_STARTED)

    def test_embedded_turn_start_takes_precedence(self) -> None:
        # A DeliveryOutcome carrying turn_start_outcome=delivered_not_started is NOT started
        # even if status looks sent — the event rail is authoritative for the turn-start.
        stdout = '{"status": "sent", "reason": "queue_enter", "turn_start_outcome": {"outcome": "delivered_not_started"}}'
        r = map_handoff_stdout_to_send_outcome(0, stdout)
        self.assertEqual(r.turn_start, TURN_START_NOT_STARTED)

    def test_blocked_is_not_started(self) -> None:
        r = map_handoff_stdout_to_send_outcome(1, '{"status": "blocked", "reason": "precondition_not_idle"}')
        self.assertEqual(r.turn_start, TURN_START_NOT_STARTED)

    def test_queue_enter_ack_is_not_started(self) -> None:
        r = map_handoff_stdout_to_send_outcome(0, '{"status": "sent", "reason": "queue_enter"}')
        self.assertEqual(r.turn_start, TURN_START_NOT_STARTED)

    def test_unparseable_is_unknown(self) -> None:
        r = map_handoff_stdout_to_send_outcome(1, "no json here at all")
        self.assertEqual(r.turn_start, TURN_START_UNKNOWN)


class ResumeHandoffSendPortTests(unittest.TestCase):
    def test_argv_and_runner_started(self) -> None:
        captured = {}

        def runner(argv):
            captured["argv"] = list(argv)
            return 0, '{"status": "sent", "reason": "ok"}'

        send = ResumeHandoffSendPort(locator="w1:p1", runner=runner).build(_done_gate(), "/repo/root", {})
        outcome = send()
        self.assertEqual(outcome.turn_start, TURN_START_STARTED)
        argv = captured["argv"]
        self.assertIn("handoff", argv)
        self.assertIn("send", argv)
        self.assertEqual(argv[argv.index("--target") + 1], "w1:p1")
        self.assertEqual(argv[argv.index("--kind") + 1], "implementation_request")
        self.assertEqual(argv[argv.index("--mode") + 1], "standard")
        self.assertEqual(argv[argv.index("--record-format") + 1], "json")
        self.assertEqual(argv[argv.index("--issue") + 1], "13760")
        # review j#79366 F1 — exact repo + lane bind, not `--target-repo auto`.
        self.assertEqual(argv[argv.index("--target-repo") + 1], "/repo/root")
        self.assertEqual(argv[argv.index("--target-lane") + 1], "lane-alpha")
        self.assertNotIn("auto", argv)

    def test_runner_raises_is_unknown_never_started(self) -> None:
        def runner(argv):
            raise RuntimeError("subprocess failed")

        send = ResumeHandoffSendPort(locator="w1:p1", runner=runner).build(_done_gate(), ".", {})
        self.assertEqual(send().turn_start, TURN_START_UNKNOWN)


class _Creds:
    def __init__(self, base_url="https://redmine.example", api_key="k"):
        self.base_url = base_url
        self.api_key = api_key


class _Transport:
    def __init__(self, raises=False):
        self.posts = []
        self._raises = raises

    def post_issue_note(self, issue_id, notes):
        if self._raises:
            raise RuntimeError("transport failed")
        self.posts.append((issue_id, notes))
        return ""


class ResumeGateRecorderTests(unittest.TestCase):
    def _rec(self, *, transport=None, creds=None):
        return ResumeGateRecorder(
            issue="13813",
            env={},
            transport_factory=lambda env: transport,
            credentials_resolver=lambda env: creds if creds is not None else _Creds(),
        )

    def test_preflight_true_when_opt_in_and_credentials(self) -> None:
        self.assertTrue(self._rec(transport=_Transport()).preflight())

    def test_preflight_false_when_write_optin_unset(self) -> None:
        self.assertFalse(self._rec(transport=None).preflight())

    def test_preflight_false_when_credentials_missing(self) -> None:
        self.assertFalse(
            self._rec(transport=_Transport(), creds=_Creds(base_url="", api_key="")).preflight()
        )

    def test_record_appends_and_confirms(self) -> None:
        transport = _Transport()
        ok = self._rec(transport=transport).record(_done_gate())
        self.assertTrue(ok)
        self.assertEqual(len(transport.posts), 1)
        issue, notes = transport.posts[0]
        self.assertEqual(issue, "13813")
        self.assertIn("[mozyo:operator-startup-gate:v=2]", notes)
        self.assertNotIn("/Users/", notes)

    def test_record_transport_failure_returns_false(self) -> None:
        self.assertFalse(self._rec(transport=_Transport(raises=True)).record(_done_gate()))

    def test_record_no_transport_returns_false(self) -> None:
        self.assertFalse(self._rec(transport=None).record(_done_gate()))


class ResumeTargetResolverTests(unittest.TestCase):
    _READY = "esc to interrupt\n> \nType your message and press enter"

    def _pin(self, locator="w1:p1"):
        return ProcessGenerationPin(
            role="implementation_worker",
            provider="claude",
            assigned_name="worker-a",
            locator=locator,
        )

    def _record(
        self, *, disposition=DISPOSITION_ACTIVE, issue="13760", generation=3, revision=1, pin=None
    ):
        return SimpleNamespace(
            lane_disposition=disposition,
            issue_id=issue,
            lane_generation=generation,
            revision=revision,
            declared_pins=(pin if pin is not None else self._pin(),),
        )

    def _identity(self):
        # The gate's exact identity — the positive workspace re-resolution returns it verbatim.
        t = _target()
        return (t.workspace_id, t.repo_identity_digest, t.execution_root)

    _UNSET = object()

    def _resolver(self, *, record=None, rows=None, workspace=_UNSET):
        rows = rows if rows is not None else [{AGENT_KEY_NAME: "worker-a", "pane_id": "w1:p1"}]
        rec = record if record is not None else self._record()
        identity = self._identity() if workspace is self._UNSET else workspace
        return ResumeTargetResolver(
            env={},
            lifecycle_get=lambda ws, lane: rec,
            inventory=lambda env: rows,
            attestation_read=lambda name: SimpleNamespace(),
            capture=lambda loc, lines: self._READY,
            workspace_resolve=lambda env: identity,
        )

    def setUp(self) -> None:
        import mozyo_bridge.core.state.herdr_identity_attestation as att

        self._att = att
        self._orig_eval = att.evaluate_attestation
        att.evaluate_attestation = lambda rec, **k: SimpleNamespace(ok=True, state="ok")
        self.addCleanup(setattr, att, "evaluate_attestation", self._orig_eval)

    def test_positive_resolution(self) -> None:
        r = self._resolver().resolve(_done_gate(), {})
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r.locator, "w1:p1")
        self.assertEqual(r.observed.target.agent_generation, 3)

    def test_generation_drift_none(self) -> None:
        self.assertIsNone(
            self._resolver(record=self._record(generation=9)).resolve(_done_gate(), {})
        )

    def test_inactive_lane_none(self) -> None:
        self.assertIsNone(
            self._resolver(record=self._record(disposition="hibernated")).resolve(_done_gate(), {})
        )

    def test_issue_binding_mismatch_none(self) -> None:
        self.assertIsNone(
            self._resolver(record=self._record(issue="99999")).resolve(_done_gate(), {})
        )

    def test_locator_mismatch_none(self) -> None:
        rows = [{AGENT_KEY_NAME: "worker-a", "pane_id": "w9:p9"}]
        self.assertIsNone(self._resolver(rows=rows).resolve(_done_gate(), {}))

    def test_duplicate_inventory_rows_none(self) -> None:
        rows = [
            {AGENT_KEY_NAME: "worker-a", "pane_id": "w1:p1"},
            {AGENT_KEY_NAME: "worker-a", "pane_id": "w1:p1"},
        ]
        self.assertIsNone(self._resolver(rows=rows).resolve(_done_gate(), {}))

    def test_missing_record_none(self) -> None:
        resolver = ResumeTargetResolver(
            env={},
            lifecycle_get=lambda ws, lane: None,
            inventory=lambda env: [],
            attestation_read=lambda name: None,
            capture=lambda loc, lines: "",
        )
        self.assertIsNone(resolver.resolve(_done_gate(), {}))

    def test_attestation_not_ok_none(self) -> None:
        self._att.evaluate_attestation = lambda rec, **k: SimpleNamespace(ok=False, state="stale")
        self.assertIsNone(self._resolver().resolve(_done_gate(), {}))

    def test_pin_identity_mismatch_none(self) -> None:
        other_pin = ProcessGenerationPin(
            role="implementation_gateway", provider="codex", assigned_name="gw", locator="w1:p1"
        )
        self.assertIsNone(
            self._resolver(record=self._record(pin=other_pin)).resolve(_done_gate(), {})
        )

    # review j#79366 F1 — the exact-binding drifts that must all fail closed.
    def test_revision_drift_none(self) -> None:
        self.assertIsNone(
            self._resolver(record=self._record(revision=999)).resolve(_done_gate(), {})
        )

    def test_foreign_provider_live_row_none(self) -> None:
        rows = [{AGENT_KEY_NAME: "worker-a", "pane_id": "w1:p1", "provider": "foreign-provider"}]
        self.assertIsNone(self._resolver(rows=rows).resolve(_done_gate(), {}))

    def test_runtime_revision_drift_none(self) -> None:
        # Declared pin runtime "declared-r1" vs a live row carrying runtime "live-r2".
        pin = ProcessGenerationPin(
            role="implementation_worker",
            provider="claude",
            assigned_name="worker-a",
            locator="w1:p1",
            runtime_revision="declared-r1",
        )
        rows = [{AGENT_KEY_NAME: "worker-a", "pane_id": "w1:p1", "runtime_revision": "live-r2"}]
        self.assertIsNone(
            self._resolver(record=self._record(pin=pin), rows=rows).resolve(_done_gate(), {})
        )

    def test_workspace_id_mismatch_none(self) -> None:
        self.assertIsNone(
            self._resolver(workspace=("foreign-ws", *self._identity()[1:])).resolve(_done_gate(), {})
        )

    def test_repo_digest_mismatch_none(self) -> None:
        wrong = (self._identity()[0], repo_identity_digest("other-repo"), ".")
        self.assertIsNone(self._resolver(workspace=wrong).resolve(_done_gate(), {}))

    def test_execution_root_mismatch_none(self) -> None:
        wrong = (self._identity()[0], self._identity()[1], "projects/elsewhere")
        self.assertIsNone(self._resolver(workspace=wrong).resolve(_done_gate(), {}))

    def test_workspace_unresolved_none(self) -> None:
        self.assertIsNone(self._resolver(workspace=None).resolve(_done_gate(), {}))

    def test_advanced_target_carries_revision(self) -> None:
        r = self._resolver().resolve(_done_gate(), {})
        assert r is not None
        self.assertEqual(r.observed.target.lane_revision, 1)


if __name__ == "__main__":
    unittest.main()
