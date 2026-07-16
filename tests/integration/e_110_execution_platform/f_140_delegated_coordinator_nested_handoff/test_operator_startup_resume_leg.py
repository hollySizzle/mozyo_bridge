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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.dispatch_outbox_fence import DispatchOutboxFence  # noqa: E402
from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DISPOSITION_ACTIVE,
    ProcessGenerationPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_producer import (  # noqa: E402
    build_v3_required_gate_from_observation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_record import (  # noqa: E402
    ResumeGateRecorder,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_send import (  # noqa: E402
    ResumeHandoffSendPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_target import (  # noqa: E402
    ResumeTargetResolver,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    AGENT_KEY_NAME,
)
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
    RESUME_EXECUTION_ROOT_UNSAFE,
    RESUME_FENCE_UNAVAILABLE,
    RESUME_LEGACY_REAPPROVAL_REQUIRED,
    RESUME_NOT_CLEAR,
    RESUME_NOT_RESUMABLE,
    RESUME_RECORDER_UNAVAILABLE,
    RESUME_SKIPPED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402
    operator_startup_resume_send as _resume_send,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_leg import (  # noqa: E402
    GATE_JOURNAL_MARKER,
    GATE_JOURNAL_MARKER_PREFIX,
    GATE_READ_CORRUPT,
    GATE_READ_GATE,
    GATE_READ_LEGACY,
    GATE_READ_NONE,
    GATE_READ_UNREADABLE,
    LatestGateRead,
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
        runtime_role="claude",
        agent_generation=3,
        lane_revision=1,
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


def _legacy_note(version: int) -> str:
    """A STRUCTURALLY READABLE legacy (v1/v2) gate note: the v3 record shape minus ``runtime_role``
    (which legacy predates), stamped with the legacy schema_version + marker."""
    import json

    record = _done_gate().to_record()
    record["schema_version"] = version
    record["target"].pop("runtime_role", None)
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return f"prior legacy gate\n{GATE_JOURNAL_MARKER_PREFIX}{version}]\n{payload}"


class _Entry:
    def __init__(self, notes: str):
        self.notes = notes


class _Recorder:
    """A gate recorder fake: preflight()/record()/record_reissue() with injectable outcomes."""

    def __init__(self, *, preflight_ok=True, record_ok=True):
        self.recorded = []
        self.reissued = []  # (gate, supersedes_note) captured by record_reissue
        self._preflight_ok = preflight_ok
        self._record_ok = record_ok
        self.preflight_calls = 0

    def preflight(self) -> bool:
        self.preflight_calls += 1
        return self._preflight_ok

    def record(self, gate) -> bool:
        self.recorded.append(gate)
        return self._record_ok

    def record_reissue(self, gate, supersedes_note) -> bool:
        self.reissued.append((gate, supersedes_note))
        return self._record_ok


class _CountingSend:
    def __init__(self):
        self.calls = 0
        self.locators = []

    def factory(self, gate, locator, repo_root, env):
        def _send():
            self.calls += 1
            self.locators.append(locator)
            return SendOutcome(turn_start=TURN_START_STARTED)

        return _send


def _exploding_send_factory(gate, locator, repo_root, env):
    def _send():
        raise AssertionError("send must not be called on a zero-send path")

    return _send


def _resolver(read_content, target=None, locator="w1:p1"):
    def _resolve(gate, env):
        return ObservedTargetResolution(
            observed=ObservedStartupTarget(
                resolution=RESOLUTION_RESOLVED, target=target if target is not None else _target()
            ),
            read_visible=lambda: read_content,
            profile_version="2",
            classifier_version="1",
            locator=locator,
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
        # Unrelated (marker-absent) newest entry is skipped; the newest gate record wins.
        entries = [
            _Entry("no gate here"),
            _Entry(render_gate_journal(_done_gate())),
            _Entry("later unrelated note"),
        ]
        read = parse_latest_gate(entries)
        self.assertEqual(read.status, GATE_READ_GATE)
        self.assertEqual(read.gate, _done_gate())

    def test_no_gate_marker_anywhere_is_none_status(self) -> None:
        read = parse_latest_gate([_Entry("just prose"), _Entry("more prose")])
        self.assertEqual(read.status, GATE_READ_NONE)
        self.assertIsNone(read.gate)

    def test_newest_malformed_gate_is_corrupt_not_older_fallback(self) -> None:
        # Finding 3 (j#79309): an older valid gate + a NEWER gate-marker entry that is
        # malformed must fail closed (corrupt), NOT fall back to the older resumable gate.
        entries = [
            _Entry(render_gate_journal(_done_gate())),  # older, valid, resumable
            _Entry(f"newest transition\n{GATE_JOURNAL_MARKER}\n{{corrupt json here"),
        ]
        read = parse_latest_gate(entries)
        self.assertEqual(read.status, GATE_READ_CORRUPT)
        self.assertIsNone(read.gate)

    def test_newest_schema_invalid_gate_is_corrupt(self) -> None:
        # A newest CURRENT-version (v3) record whose JSON parses but fails the schema invariants
        # is corrupt (fail-closed), NOT resumed.
        bad = f"{GATE_JOURNAL_MARKER}\n" + '{"schema_version": 3, "state": "consumed"}'
        entries = [_Entry(render_gate_journal(_done_gate())), _Entry(bad)]
        self.assertEqual(parse_latest_gate(entries).status, GATE_READ_CORRUPT)

    def test_newest_readable_legacy_v2_gate_is_legacy_not_corrupt_not_gate(self) -> None:
        # j#79405 §B / j#79481 F1: a STRUCTURALLY READABLE legacy v2 record (marker v=2) is
        # classified LEGACY — reapproval required — never CORRUPT, never parsed as a current gate,
        # and never falls back to an older resumable gate (version-agnostic prefix still detects it).
        entries = [_Entry(render_gate_journal(_done_gate())), _Entry(_legacy_note(2))]
        read = parse_latest_gate(entries)
        self.assertEqual(read.status, GATE_READ_LEGACY)
        self.assertIsNone(read.gate)

    def test_readable_legacy_v1_gate_is_legacy(self) -> None:
        self.assertEqual(parse_latest_gate([_Entry(_legacy_note(1))]).status, GATE_READ_LEGACY)

    def test_malformed_legacy_fragment_is_corrupt_not_legacy(self) -> None:
        # j#79481 F1: a bare {"schema_version": 2} fragment is NOT a readable legacy record — it is
        # corrupt (fail-closed), distinct from a real legacy gate.
        bare = f"{GATE_JOURNAL_MARKER_PREFIX}2]\n" + '{"schema_version": 2, "state": "required"}'
        self.assertEqual(parse_latest_gate([_Entry(bare)]).status, GATE_READ_CORRUPT)

    def test_v2_to_v3_supersession_newest_v3_wins(self) -> None:
        # A fresh v3 gate recorded AFTER a legacy v2 gate supersedes it: the newest (v3) record
        # decides the read, so a re-approved v3 gate resumes normally.
        entries = [_Entry(_legacy_note(2)), _Entry(render_gate_journal(_done_gate()))]
        read = parse_latest_gate(entries)
        self.assertEqual(read.status, GATE_READ_GATE)
        self.assertEqual(read.gate, _done_gate())

    def test_malformed_payload_is_none(self) -> None:
        self.assertIsNone(parse_gate_from_note(f"header\n{GATE_JOURNAL_MARKER}\n{{not json"))
        self.assertIsNone(parse_gate_from_note("no marker at all"))
        # A legacy v2 payload is not a CURRENT gate -> parse_gate_from_note returns None.
        self.assertIsNone(
            parse_gate_from_note(f"{GATE_JOURNAL_MARKER_PREFIX}2]\n" + '{"schema_version": 2}')
        )


class ResumeLegTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.fence = DispatchOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.args = argparse.Namespace(repo=str(self.home))

    def _run(
        self,
        *,
        gate_source,
        target_resolver,
        send_factory,
        gate_recorder,
        legacy_reissuer=None,
        observed_at="2026-07-16T01:00:00Z",
    ):
        return execute_startup_resume(
            self.args,
            "13813",
            env={},
            observed_at=observed_at,
            gate_source=gate_source,
            target_resolver=target_resolver,
            send_factory=send_factory,
            gate_recorder=gate_recorder,
            legacy_reissuer=legacy_reissuer,
            fence=self.fence,
        )

    def test_recorder_preflight_unavailable_is_zero_send_before_reserve(self) -> None:
        # j#79332 §5: the durable writer is preflighted BEFORE the reserve; an unavailable
        # writer means reserve/send 0 (the send could never be durably recorded).
        rec = _Recorder(preflight_ok=False)
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
            target_resolver=_resolver(_READY),
            send_factory=_exploding_send_factory,
            gate_recorder=rec,
        )
        self.assertEqual(result.result, RESUME_RECORDER_UNAVAILABLE)
        self.assertEqual(rec.preflight_calls, 1)
        self.assertEqual(len(rec.recorded), 0)

    def test_send_receives_the_resolved_locator(self) -> None:
        send = _CountingSend()
        self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
            target_resolver=_resolver(_READY, locator="w7:pQ"),
            send_factory=send.factory,
            gate_recorder=_Recorder(),
        )
        self.assertEqual(send.locators, ["w7:pQ"])

    def test_record_failure_post_send_is_reconcile_not_resend(self) -> None:
        # A delivered send whose durable append fails: the send is fenced exactly-once, so a
        # record failure is a typed record_failed / reconcile, never a re-send.
        send = _CountingSend()
        rec = _Recorder(record_ok=False)
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
            target_resolver=_resolver(_READY),
            send_factory=send.factory,
            gate_recorder=rec,
        )
        self.assertEqual(result.result, RESUME_DELIVERED)
        self.assertTrue(result.record_failed)
        self.assertTrue(result.needs_reconcile)
        self.assertEqual(send.calls, 1)
        self.assertEqual(len(rec.recorded), 1)  # record was attempted (and failed)

    def test_positive_delivers_once_and_records_consumed(self) -> None:
        send = _CountingSend()
        rec = _Recorder()
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
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
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
            target_resolver=_resolver(_READY),
            send_factory=send.factory,
            gate_recorder=_Recorder(),
        )
        second = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
            target_resolver=_resolver(_READY),
            send_factory=send.factory,  # would raise via count? no — assert via calls
            gate_recorder=_Recorder(),
        )
        self.assertEqual(second.result, RESUME_SKIPPED)
        self.assertEqual(send.calls, 1)  # still exactly one across both runs

    def test_still_blocked_screen_is_zero_send_unrecorded(self) -> None:
        rec = _Recorder()
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
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
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
            target_resolver=_resolver(_READY, target=_target(lane_id="lane-beta")),
            send_factory=_exploding_send_factory,
            gate_recorder=rec,
        )
        self.assertEqual(result.result, RESUME_NOT_CLEAR)
        self.assertEqual(result.projection_disposition, PROJECT_IDENTITY_MISMATCH)
        self.assertEqual(len(rec.recorded), 0)

    def test_missing_durable_gate_is_not_resumable(self) -> None:
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_NONE),
            target_resolver=_resolver(_READY),
            send_factory=_exploding_send_factory,
            gate_recorder=_Recorder(),
        )
        self.assertEqual(result.result, RESUME_NOT_RESUMABLE)

    def test_corrupt_latest_gate_is_zero_send_no_fallback(self) -> None:
        # Finding 3 (j#79309): a corrupt latest gate record must fail closed (zero-send),
        # never resume — the leg does NOT read the pane, resolve, or send.
        rec = _Recorder()
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_CORRUPT),
            target_resolver=lambda gate, env: (_ for _ in ()).throw(
                AssertionError("resolver must not run on a corrupt gate")
            ),
            send_factory=_exploding_send_factory,
            gate_recorder=rec,
        )
        self.assertEqual(result.result, RESUME_NOT_RESUMABLE)
        self.assertIn("corrupt", result.detail)
        self.assertEqual(len(rec.recorded), 0)

    def test_unreadable_latest_gate_is_zero_send(self) -> None:
        # review j#79504 F1: an UNREADABLE ticket-provider read is indeterminate -> fail closed with
        # zero actuation (never conflated with no_gate, never resolves/sends).
        rec = _Recorder()
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_UNREADABLE),
            target_resolver=lambda gate, env: (_ for _ in ()).throw(
                AssertionError("resolver must not run on an unreadable read")
            ),
            send_factory=_exploding_send_factory,
            gate_recorder=rec,
        )
        self.assertEqual(result.result, RESUME_NOT_RESUMABLE)
        self.assertIn("unreadable", result.detail)
        self.assertEqual(len(rec.recorded), 0)

    def test_unresolved_live_target_is_zero_send(self) -> None:
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
            target_resolver=lambda gate, env: None,  # cannot resolve the live target
            send_factory=_exploding_send_factory,
            gate_recorder=_Recorder(),
        )
        self.assertEqual(result.result, RESUME_NOT_CLEAR)
        self.assertEqual(result.projection_disposition, PROJECT_IDENTITY_UNRESOLVED)

    def test_legacy_latest_gate_reissue_unavailable_is_manual_reapproval_zero_send(self) -> None:
        # j#79405 §B: a readable legacy latest routes to reapproval — reserve/send 0, no resume of
        # the legacy gate. When no fresh observation can be re-observed (reissuer -> None), the leg
        # guides a MANUAL reapproval and records nothing.
        rec = _Recorder()
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_LEGACY, legacy_record={"gate_id": "g"}),
            target_resolver=lambda gate, env: (_ for _ in ()).throw(
                AssertionError("resolver must not run on a legacy gate")
            ),
            send_factory=_exploding_send_factory,
            gate_recorder=rec,
            legacy_reissuer=lambda legacy_record, issue, repo_root: None,
        )
        self.assertEqual(result.result, RESUME_LEGACY_REAPPROVAL_REQUIRED)
        self.assertFalse(result.sent)
        self.assertEqual(len(rec.recorded), 0)
        self.assertEqual(len(rec.reissued), 0)

    def test_legacy_latest_gate_reissues_fresh_v3_with_supersedes(self) -> None:
        # review j#79504 F2: a readable legacy latest re-observes a FRESH v3 required gate via the
        # producer-backed reissuer and durably records it (record_reissue) with a supersedes pointer
        # naming the legacy gate — reserve/send 0, no legacy backfill, awaiting fresh owner approval.
        rec = _Recorder()
        fresh = build_required_gate(
            gate_id="gate-1",
            action_generation=2,
            original_request=_original(),
            target=_target(),
            classification=_classification(),
        )
        legacy_record = {"gate_id": "gate-1", "action_generation": 1}
        result = self._run(
            gate_source=lambda issue: LatestGateRead(GATE_READ_LEGACY, legacy_record=legacy_record),
            target_resolver=lambda gate, env: (_ for _ in ()).throw(
                AssertionError("resolver must not run on a legacy gate")
            ),
            send_factory=_exploding_send_factory,
            gate_recorder=rec,
            legacy_reissuer=lambda lr, issue, repo_root: fresh,
        )
        self.assertEqual(result.result, RESUME_LEGACY_REAPPROVAL_REQUIRED)
        self.assertFalse(result.sent)
        self.assertEqual(len(rec.recorded), 0)  # no same-gate transition
        self.assertEqual(len(rec.reissued), 1)  # fresh v3 gate durably recorded
        recorded_gate, supersedes_note = rec.reissued[0]
        self.assertEqual(recorded_gate, fresh)
        self.assertIn("gate=gate-1", supersedes_note)
        self.assertIn("action_generation=1", supersedes_note)  # names the superseded legacy gen

    def test_default_legacy_reissuer_invokes_producer_from_observation(self) -> None:
        # review j#79504 F2: the DEFAULT (production) reissuer is a real call-site of the producer.
        # A seeded lifecycle observation + provider binding -> a fresh v3 required gate whose runtime
        # identity comes from the declared pin (fail-closed sub-seams only: lifecycle / binding read).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            operator_startup_resume_leg as leg_mod,
            operator_startup_resume_target as tgt_mod,
            workflow_binding_source as bind_mod,
        )

        class _Binding:
            def provider_for(self, role):
                return "claude"

        record = SimpleNamespace(
            repo_workspace_id="ws-alpha",
            lane_id="lane-alpha",
            lane_generation=3,
            revision=1,
            declared_pins=(
                ProcessGenerationPin(
                    role="claude", provider="claude", assigned_name="worker-a", locator="w1:p1"
                ),
            ),
        )
        orig_lc = tgt_mod._default_lifecycle_get
        orig_bind = bind_mod.load_workflow_binding
        tgt_mod._default_lifecycle_get = lambda ws, lane: record
        bind_mod.load_workflow_binding = lambda repo_root=None: (_Binding(), ())
        try:
            legacy_record = {
                "gate_id": "gate-1",
                "action_generation": 1,
                "target": {
                    "workspace_id": "ws-alpha",
                    "lane_id": "lane-alpha",
                    "target_role": "implementation_worker",
                    "execution_root": ".",
                },
                "original_request": {
                    "source": "redmine", "issue": "13760", "journal": "77948", "delivery_id": "deliv-1"
                },
                "classification": {
                    "blocker_id": "first_run_theme",
                    "profile_version": "2",
                    "classifier_version": "1",
                    "observed_at": "x",
                },
            }
            fresh = leg_mod._default_legacy_reissuer(legacy_record, "13813", str(self.home))
        finally:
            tgt_mod._default_lifecycle_get = orig_lc
            bind_mod.load_workflow_binding = orig_bind
        self.assertIsNotNone(fresh)
        assert fresh is not None
        self.assertEqual(fresh.state, "required")
        self.assertEqual(fresh.target.runtime_role, "claude")  # from the declared pin
        self.assertEqual(fresh.target.target_role, "implementation_worker")  # workflow role
        self.assertEqual(fresh.action_generation, 2)  # legacy generation + 1

    def test_unsafe_execution_root_is_zero_send_before_reserve(self) -> None:
        # j#79405 §C: an execution_root that does not safely resolve under the action-time repo
        # root fails closed BEFORE the reserve (defense-in-depth; the domain already rejects `..`).
        rec = _Recorder()
        send = _CountingSend()
        orig = _resume_send.resolve_execution_workdir
        _resume_send.resolve_execution_workdir = lambda repo_root, execution_root: None
        try:
            result = self._run(
                gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
                target_resolver=_resolver(_READY),
                send_factory=send.factory,
                gate_recorder=rec,
            )
        finally:
            _resume_send.resolve_execution_workdir = orig
        self.assertEqual(result.result, RESUME_EXECUTION_ROOT_UNSAFE)
        self.assertEqual(send.calls, 0)
        self.assertEqual(len(rec.recorded), 0)

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
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
            target_resolver=_resolver(_READY),
            send_factory=_exploding_send_factory,
            gate_recorder=_Recorder(),
            fence=lost,
        )
        self.assertEqual(result.result, RESUME_FENCE_UNAVAILABLE)


class _Creds:
    def __init__(self, base_url="https://redmine.example", api_key="k"):
        self.base_url = base_url
        self.api_key = api_key


class _Transport:
    def __init__(self):
        self.posts = []

    def post_issue_note(self, issue_id, notes):
        self.posts.append((issue_id, notes))
        return ""


class ProductionCompositionTests(unittest.TestCase):
    """Top-level `execute_startup_resume` driven through the REAL production composition —
    the real ResumeTargetResolver (real evaluate_attestation, a production-shape declared
    ProcessGenerationPin whose role is the runtime role "claude", a raw inventory row), the real
    ResumeHandoffSendPort, and the real ResumeGateRecorder — with ONLY leaf sub-seams faked
    (runner / ticket writer / lifecycle / inventory / attestation read / workspace / binding).
    This is the composition the reviewer (j#79392 F1 / j#79405 §D) required: production-shape
    inputs, positive send=1, no positive-stubbed attestation or workflow-role-shaped pin.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.fence = DispatchOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.args = argparse.Namespace(repo=str(self.home))

    def _production_resolver(self):
        record = SimpleNamespace(
            lane_disposition=DISPOSITION_ACTIVE,
            issue_id="13760",
            lane_generation=3,
            revision=1,
            declared_pins=(
                # RUNTIME-role pin (role == provider token "claude"), NOT the workflow role.
                ProcessGenerationPin(
                    role="claude", provider="claude", assigned_name="worker-a", locator="w1:p1"
                ),
            ),
        )
        attestation = IdentityAttestationRecord(
            assigned_name="worker-a",
            workspace_id="ws-alpha",
            role="claude",
            lane_id="lane-alpha",
            locator="w1:p1",
            verdict=VERDICT_PRESENT,
        )
        t = _target()
        return ResumeTargetResolver(
            env={},
            repo_root=str(self.home),
            lifecycle_get=lambda ws, lane: record,
            inventory=lambda env: [{AGENT_KEY_NAME: "worker-a", "pane_id": "w1:p1"}],
            attestation_read=lambda name: attestation,  # REAL record -> REAL evaluate_attestation
            capture=lambda loc, lines: _READY,
            workspace_resolve=lambda repo_root, execution_root, env: (
                t.workspace_id, t.repo_identity_digest, t.execution_root
            ),
            binding_resolve=lambda role, repo_root, env: "claude",
        )

    def test_production_composition_delivers_once_workdir_and_records_consumed(self) -> None:
        runner_calls = []

        def _runner(argv):
            runner_calls.append(list(argv))
            return 0, '{"status": "sent", "reason": "ok"}'

        transport = _Transport()
        recorder = ResumeGateRecorder(
            issue="13813",
            env={},
            transport_factory=lambda env: transport,
            credentials_resolver=lambda env: _Creds(),
        )
        result = execute_startup_resume(
            self.args,
            "13813",
            env={},
            observed_at="2026-07-16T01:00:00Z",
            gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
            target_resolver=self._production_resolver().resolve,
            send_factory=lambda gate, locator, repo_root, env: ResumeHandoffSendPort(
                locator=locator, runner=_runner
            ).build(gate, repo_root, env),
            gate_recorder=recorder,
            fence=self.fence,
        )
        # positive send=1, exactly-once, gate advanced to consumed and durably recorded.
        self.assertEqual(result.result, RESUME_DELIVERED)
        self.assertTrue(result.sent)
        self.assertEqual(len(runner_calls), 1)
        argv = runner_calls[0]
        # the exact repo/lane/workdir bind rode the single high-level send. repo_root is the
        # arg-resolved root (symlinks resolved); workdir == repo_root for execution_root '.'.
        resolved_root = str(Path(self.home).resolve())
        self.assertEqual(argv[argv.index("--target-repo") + 1], resolved_root)
        self.assertEqual(argv[argv.index("--target-lane") + 1], "lane-alpha")
        self.assertEqual(argv[argv.index("--workdir") + 1], resolved_root)
        self.assertEqual(
            argv[argv.index("--workdir") + 1], argv[argv.index("--target-repo") + 1]
        )
        self.assertEqual(argv[argv.index("--target") + 1], "w1:p1")
        # durable append-only transition recorded (consumed).
        self.assertEqual(len(transport.posts), 1)
        self.assertEqual(transport.posts[0][0], "13813")
        self.assertIn(GATE_JOURNAL_MARKER, transport.posts[0][1])

    def test_producer_to_journal_to_resume_delivers_once(self) -> None:
        # F2 (j#79481): the authoritative producer builds a v3 gate from ONE lifecycle observation;
        # that gate round-trips through the journal and the SAME observation resolves the live
        # target -> send=1. Proves producer -> journal -> resume end-to-end (no hand-assembled target).
        class _Binding:
            def provider_for(self, role):
                return {"implementation_worker": "claude"}.get(role)

        record = SimpleNamespace(
            repo_workspace_id="ws-alpha",
            lane_id="lane-alpha",
            lane_generation=3,
            revision=1,
            lane_disposition=DISPOSITION_ACTIVE,
            issue_id="13760",
            declared_pins=(
                ProcessGenerationPin(
                    role="claude", provider="claude", assigned_name="worker-a", locator="w1:p1"
                ),
            ),
        )
        produced = build_v3_required_gate_from_observation(
            record=record,
            binding=_Binding(),
            workflow_role="implementation_worker",
            execution_root=".",
            gate_id="gate-prod",
            action_generation=1,
            original_request=_original(),
            classification=_classification(),
        )
        # Owner approves + operator reports done -> the resumable durable gate.
        done = report_operator_done(
            approve_gate(produced, approval=GateApproval(source_journal="78412"))
        )
        # Round-trip through the durable journal.
        read = parse_latest_gate([_Entry(render_gate_journal(done))])
        self.assertEqual(read.status, GATE_READ_GATE)
        self.assertEqual(read.gate, done)

        attestation = IdentityAttestationRecord(
            assigned_name="worker-a",
            workspace_id="ws-alpha",
            role="claude",
            lane_id="lane-alpha",
            locator="w1:p1",
            verdict=VERDICT_PRESENT,
        )
        resolver = ResumeTargetResolver(
            env={},
            repo_root=str(self.home),
            lifecycle_get=lambda ws, lane: record,
            inventory=lambda env: [{AGENT_KEY_NAME: "worker-a", "pane_id": "w1:p1"}],
            attestation_read=lambda name: attestation,
            capture=lambda loc, lines: _READY,
            workspace_resolve=lambda repo_root, execution_root, env: (
                done.target.workspace_id, done.target.repo_identity_digest, done.target.execution_root
            ),
            binding_resolve=lambda role, repo_root, env: "claude",
        )
        result = execute_startup_resume(
            self.args,
            "13813",
            env={},
            observed_at="2026-07-16T01:00:00Z",
            gate_source=lambda issue: read,
            target_resolver=resolver.resolve,
            send_factory=lambda gate, locator, repo_root, env: ResumeHandoffSendPort(
                locator=locator, runner=lambda argv: (0, '{"status": "sent", "reason": "ok"}')
            ).build(gate, repo_root, env),
            gate_recorder=ResumeGateRecorder(
                issue="13813",
                env={},
                transport_factory=lambda env: _Transport(),
                credentials_resolver=lambda env: _Creds(),
            ),
            fence=self.fence,
        )
        self.assertEqual(result.result, RESUME_DELIVERED)
        self.assertTrue(result.sent)

    def test_production_composition_rerun_sends_zero(self) -> None:
        def _runner(argv):
            return 0, '{"status": "sent", "reason": "ok"}'

        def _run_once():
            return execute_startup_resume(
                self.args,
                "13813",
                env={},
                observed_at="2026-07-16T01:00:00Z",
                gate_source=lambda issue: LatestGateRead(GATE_READ_GATE, _done_gate()),
                target_resolver=self._production_resolver().resolve,
                send_factory=lambda gate, locator, repo_root, env: ResumeHandoffSendPort(
                    locator=locator, runner=_runner
                ).build(gate, repo_root, env),
                gate_recorder=ResumeGateRecorder(
                    issue="13813",
                    env={},
                    transport_factory=lambda env: _Transport(),
                    credentials_resolver=lambda env: _Creds(),
                ),
                fence=self.fence,
            )

        first = _run_once()
        second = _run_once()
        self.assertEqual(first.result, RESUME_DELIVERED)
        self.assertEqual(second.result, RESUME_SKIPPED)  # fence refuses the re-reserve


if __name__ == "__main__":
    unittest.main()
