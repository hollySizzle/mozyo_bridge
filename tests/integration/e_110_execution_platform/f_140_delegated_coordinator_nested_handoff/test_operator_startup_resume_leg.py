"""Action-time live resume leg integration (Redmine #13813, review j#79268 Finding 1).

Drives :func:`execute_startup_resume` end-to-end with a real temp fence and injected ports
(gate source / target resolver / send factory / gate recorder) — the analogue of #13489's
:mod:`test_herdr_dispatch_cli_leg`. Proves the leg wires the four required seams (j#79214
items 1/3/6): it re-reads the latest durable gate from the ticket-provider port, re-resolves
the live target at action time, drives the exactly-once orchestrator, and records the
append-only transition — and that action-time drift / a still-blocked screen / a missing gate
/ a lost fence all fail closed with zero send. The durable gate journal serialization round-
trips and is redaction-safe.

Injected fakes only; no live tmux, Redmine, or handoff rail.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.dispatch_outbox_fence import DispatchOutboxFence  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (  # noqa: E402
    SendOutcome,
    TURN_START_STARTED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection import (  # noqa: E402
    PROJECT_IDENTITY_MISMATCH,
    PROJECT_IDENTITY_UNRESOLVED,
    PROJECT_OPERATOR_ACTION_REQUIRED,
    RESOLUTION_RESOLVED,
    ObservedStartupTarget,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume import (  # noqa: E402
    RESUME_DELIVERED,
    RESUME_FENCE_UNAVAILABLE,
    RESUME_NOT_CLEAR,
    RESUME_NOT_RESUMABLE,
    RESUME_SKIPPED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_leg import (  # noqa: E402
    GATE_JOURNAL_MARKER,
    ObservedTargetResolution,
    execute_startup_resume,
    parse_gate_from_note,
    parse_latest_gate,
    render_gate_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (  # noqa: E402
    STATE_CONSUMED,
    GateApproval,
    GateClassification,
    GateTarget,
    OriginalRequest,
    build_required_gate,
    repo_identity_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate_lattice import (  # noqa: E402
    approve_gate,
    consume_gate,
    report_operator_done,
    verify_clear_gate,
)

_READY = "esc to interrupt\n> \nType your message and press enter"
_THEME = (
    "Let's get started\n"
    "Choose the text style that looks best with your terminal\n"
    "> Dark mode"
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
    )
    kwargs.update(overrides)
    return GateTarget(**kwargs)


def _original() -> OriginalRequest:
    return OriginalRequest(
        source="redmine", issue="13760", journal="77948", delivery_id="deliv-1"
    )


def _classification() -> GateClassification:
    return GateClassification(
        blocker_id="first_run_theme",
        profile_version="2",
        classifier_version="1",
        observed_at="2026-07-15T00:00:00Z",
    )


def _done_gate():
    return report_operator_done(
        approve_gate(
            build_required_gate(
                gate_id="gate-1",
                action_generation=1,
                original_request=_original(),
                target=_target(),
                classification=_classification(),
            ),
            approval=GateApproval(source_journal="78412"),
        )
    )


class _Entry:
    def __init__(self, notes: str):
        self.notes = notes


class _Recorder:
    def __init__(self):
        self.recorded = []

    def __call__(self, gate):
        self.recorded.append(gate)


class _CountingSend:
    def __init__(self):
        self.calls = 0

    def factory(self, gate, repo_root, env):
        def _send():
            self.calls += 1
            return SendOutcome(turn_start=TURN_START_STARTED)

        return _send


def _exploding_send_factory(gate, repo_root, env):
    def _send():
        raise AssertionError("send must not be called on a zero-send path")

    return _send


def _resolver(read_content, target=None):
    def _resolve(gate, env):
        return ObservedTargetResolution(
            observed=ObservedStartupTarget(
                resolution=RESOLUTION_RESOLVED, target=target if target is not None else _target()
            ),
            read_visible=lambda: read_content,
            profile_version="2",
            classifier_version="1",
        )

    return _resolve


class GateJournalSerializationTests(unittest.TestCase):
    def test_required_gate_round_trips(self) -> None:
        gate = build_required_gate(
            gate_id="gate-1",
            action_generation=1,
            original_request=_original(),
            target=_target(),
            classification=_classification(),
        )
        note = render_gate_journal(gate)
        self.assertIn(GATE_JOURNAL_MARKER, note)
        self.assertEqual(parse_gate_from_note(note), gate)

    def test_advanced_gate_round_trips(self) -> None:
        consumed = consume_gate(
            verify_clear_gate(
                _done_gate(),
                startup_clear_observed_at="2026-07-16T01:00:00Z",
                dispatch_fence_state="reserved",
            ),
            consumed_delivery_record="deliv-1",
        )
        self.assertEqual(parse_gate_from_note(render_gate_journal(consumed)), consumed)

    def test_note_is_path_and_secret_safe(self) -> None:
        note = render_gate_journal(_done_gate())
        self.assertNotIn("/Users/", note)
        self.assertNotIn("api_key", note)
        self.assertNotIn("password", note)

    def test_parse_latest_gate_newest_first(self) -> None:
        entries = [
            _Entry("no gate here"),
            _Entry(render_gate_journal(_done_gate())),
            _Entry("later unrelated note"),
        ]
        self.assertEqual(parse_latest_gate(entries), _done_gate())

    def test_malformed_payload_is_none(self) -> None:
        self.assertIsNone(parse_gate_from_note(f"header\n{GATE_JOURNAL_MARKER}\n{{not json"))
        self.assertIsNone(parse_gate_from_note("no marker at all"))


class ResumeLegTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.fence = DispatchOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.args = argparse.Namespace(repo=str(self.home))

    def _run(self, *, gate_source, target_resolver, send_factory, gate_recorder, observed_at="2026-07-16T01:00:00Z"):
        return execute_startup_resume(
            self.args,
            "13813",
            env={},
            observed_at=observed_at,
            gate_source=gate_source,
            target_resolver=target_resolver,
            send_factory=send_factory,
            gate_recorder=gate_recorder,
            fence=self.fence,
        )

    def test_positive_delivers_once_and_records_consumed(self) -> None:
        send = _CountingSend()
        rec = _Recorder()
        result = self._run(
            gate_source=lambda issue: _done_gate(),
            target_resolver=_resolver(_READY),
            send_factory=send.factory,
            gate_recorder=rec,
        )
        self.assertEqual(result.result, RESUME_DELIVERED)
        self.assertEqual(send.calls, 1)  # send=1
        self.assertEqual(len(rec.recorded), 1)  # append-only transition recorded
        self.assertEqual(rec.recorded[0].state, STATE_CONSUMED)

    def test_duplicate_rerun_through_leg_sends_zero(self) -> None:
        send = _CountingSend()
        self._run(
            gate_source=lambda issue: _done_gate(),
            target_resolver=_resolver(_READY),
            send_factory=send.factory,
            gate_recorder=_Recorder(),
        )
        second = self._run(
            gate_source=lambda issue: _done_gate(),
            target_resolver=_resolver(_READY),
            send_factory=send.factory,  # would raise via count? no — assert via calls
            gate_recorder=_Recorder(),
        )
        self.assertEqual(second.result, RESUME_SKIPPED)
        self.assertEqual(send.calls, 1)  # still exactly one across both runs

    def test_still_blocked_screen_is_zero_send_unrecorded(self) -> None:
        rec = _Recorder()
        result = self._run(
            gate_source=lambda issue: _done_gate(),
            target_resolver=_resolver(_THEME),
            send_factory=_exploding_send_factory,
            gate_recorder=rec,
        )
        self.assertEqual(result.result, RESUME_NOT_CLEAR)
        self.assertEqual(result.projection_disposition, PROJECT_OPERATOR_ACTION_REQUIRED)
        self.assertEqual(len(rec.recorded), 0)

    def test_action_time_target_drift_is_zero_send(self) -> None:
        # The durable gate is pinned to lane-alpha; the live re-resolution names lane-beta.
        # The leg's action-time resolution must turn this drift into zero send.
        rec = _Recorder()
        result = self._run(
            gate_source=lambda issue: _done_gate(),
            target_resolver=_resolver(_READY, target=_target(lane_id="lane-beta")),
            send_factory=_exploding_send_factory,
            gate_recorder=rec,
        )
        self.assertEqual(result.result, RESUME_NOT_CLEAR)
        self.assertEqual(result.projection_disposition, PROJECT_IDENTITY_MISMATCH)
        self.assertEqual(len(rec.recorded), 0)

    def test_missing_durable_gate_is_not_resumable(self) -> None:
        result = self._run(
            gate_source=lambda issue: None,
            target_resolver=_resolver(_READY),
            send_factory=_exploding_send_factory,
            gate_recorder=_Recorder(),
        )
        self.assertEqual(result.result, RESUME_NOT_RESUMABLE)

    def test_unresolved_live_target_is_zero_send(self) -> None:
        result = self._run(
            gate_source=lambda issue: _done_gate(),
            target_resolver=lambda gate, env: None,  # cannot resolve the live target
            send_factory=_exploding_send_factory,
            gate_recorder=_Recorder(),
        )
        self.assertEqual(result.result, RESUME_NOT_CLEAR)
        self.assertEqual(result.projection_disposition, PROJECT_IDENTITY_UNRESOLVED)

    def test_lost_fence_is_fail_closed_no_send(self) -> None:
        # A store LOSS (sidecar remains, DB gone) must fail bootstrap closed with no send —
        # the leg never silently re-creates a fresh store that could re-send (deletion-safe).
        lost = DispatchOutboxFence(home=Path(self._tmp.name) / "lost")
        lost.bootstrap()
        lost.path.unlink()  # DB gone, sidecar remains -> inconsistent one-sided store
        result = execute_startup_resume(
            self.args,
            "13813",
            env={},
            observed_at="2026-07-16T01:00:00Z",
            gate_source=lambda issue: _done_gate(),
            target_resolver=_resolver(_READY),
            send_factory=_exploding_send_factory,
            gate_recorder=_Recorder(),
            fence=lost,
        )
        self.assertEqual(result.result, RESUME_FENCE_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
