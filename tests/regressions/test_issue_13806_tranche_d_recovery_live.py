"""Redmine #13806 tranche D R1-F1 — live stale-worker recovery adapters (isolated runtime).

The public ``sublane recover-stale`` command must actually observe the live inventory and drive
the real close/launch/attest + redispatch (review j#79528 F1). These tests pin the live adapters
with an ISOLATED / fake herdr runtime (a fake ``agent list`` + an isolated delivery ledger) — no
real managed worker is ever actuated (the tranche boundary), yet the real classification and the
real fail-closed rails are exercised: an unreadable / ambiguous inventory is never a positive
absence, a same-name recycle is never closed, and the redispatch is ledger-confirmed exactly once.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_delivery_ledger import (  # noqa: E402
    HerdrDeliveryLedger,
    HerdrDeliveryLedgerRecord,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live as live  # noqa: E402,E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E402,E501
    RecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ATTEST_BOUND,
    ATTEST_MISMATCH,
    ATTEST_PENDING,
    CLOSE_DONE,
    CLOSE_ERROR,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_ABSENT,
    OLD_SLOT_AMBIGUOUS,
    OLD_SLOT_PRESENT,
    OLD_SLOT_RECYCLED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E402,E501
    RECOVER_ACTIONABLE,
    RECOVER_BLOCK_GATEWAY_OR_FOREIGN,
    RECOVER_BLOCK_NOT_STALE,
    RECOVER_BLOCK_PRODUCTIVE,
    RECOVER_BLOCK_STALE_GENERATION,
    RECOVER_BLOCK_UNKNOWN,
    RECOVER_BLOCK_WRONG_ISSUE_LANE,
    decide_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E402,E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

WS = "wsX"
LANE = "issue_13806_x"
ROLE = "claude"
LOCATOR = "w28:p35"
NAME = encode_assigned_name(WS, ROLE, LANE)


def _row(**overrides):
    row = {
        "name": NAME,
        "pane_id": LOCATOR,
        "agent": "",  # blank => shell residue (stale)
        "status": "unknown",  # RUNTIME_UNKNOWN => not productive
        "revision": 3,
        "foreground_cwd": str(ROOT),  # a real git checkout => worktree readable
    }
    row.update(overrides)
    return row


def _request(**overrides):
    base = dict(
        issue="13806", lane=LANE, role=ROLE, provider=ROLE, assigned_name=NAME,
        locator=LOCATOR, journal="79485", action_id="", action_generation=7,
        # worker_revision (preflight generation gate) and lane_revision/lane_generation (the
        # lane-lifecycle preservation fence) are DISTINCT authorities (Redmine #13806 split):
        # the default row carries revision 3, so the worker pin matches at "3".
        worker_revision="3", lane_revision="3", lane_generation="2",
        expected_gate="implementation_request", next_semantic_action="dispatch_once",
    )
    base.update(overrides)
    return RecoveryRequest(**base)


class _LiveCase(unittest.TestCase):
    def setUp(self):
        self._orig_rows = live.list_herdr_agent_rows
        self._orig_ws = live.repo_scope_workspace_id
        live.repo_scope_workspace_id = lambda root: WS

    def tearDown(self):
        live.list_herdr_agent_rows = self._orig_rows
        live.repo_scope_workspace_id = self._orig_ws

    def _ops(self, rows):
        live.list_herdr_agent_rows = lambda env: rows
        return live.LiveStaleWorkerRecoveryOps(repo_root=ROOT, request=_request())


class ObserveTargetTests(_LiveCase):
    def test_full_stale_row_is_actionable(self):
        obs = self._ops([_row()]).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_ACTIONABLE)

    def test_no_rows_is_identity_unknown(self):
        obs = self._ops([]).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_UNKNOWN)

    def test_ambiguous_name_is_identity_unknown(self):
        rows = [_row(), _row(pane_id="w28:p99")]  # same name, two locators
        obs = self._ops(rows).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_UNKNOWN)

    def test_default_lane_is_gateway_or_foreign(self):
        name = encode_assigned_name(WS, ROLE, "default")
        obs = self._ops([_row(name=name)]).observe_target(
            _request(lane="default", assigned_name=name)
        )
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_GATEWAY_OR_FOREIGN)

    def test_same_issue_lane_gateway_is_gateway_or_foreign(self):
        # R2-F1: the same-issue-lane Codex GATEWAY sits in a non-default lane but is the gateway
        # provider — it must be protected, never classified as a standard worker.
        gw_name = encode_assigned_name(WS, "codex", LANE)
        row = {
            "name": gw_name, "pane_id": "w28:p34", "agent": "", "status": "unknown",
            "revision": 3, "foreground_cwd": str(ROOT),
        }
        live.list_herdr_agent_rows = lambda env: [row]
        ops = live.LiveStaleWorkerRecoveryOps(repo_root=ROOT, request=_request())
        obs = ops.observe_target(
            _request(role="codex", provider="codex", assigned_name=gw_name, locator="w28:p34")
        )
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_GATEWAY_OR_FOREIGN)

    def test_approval_provider_gateway_is_protected(self):
        # R2-R1: a live Claude worker row but an approval whose provider pins the GATEWAY must be
        # protected — the provider field enters the transaction authority and is not observable
        # downstream, so it is validated here.
        obs = self._ops([_row()]).observe_target(_request(provider="codex"))
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_GATEWAY_OR_FOREIGN)

    def test_approval_provider_foreign_is_protected(self):
        obs = self._ops([_row()]).observe_target(_request(provider="rustc"))
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_GATEWAY_OR_FOREIGN)

    def test_approval_provider_blank_is_protected(self):
        obs = self._ops([_row()]).observe_target(_request(provider=""))
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_GATEWAY_OR_FOREIGN)

    def test_wrong_issue_lane(self):
        name = encode_assigned_name(WS, ROLE, "issue_99999_other")
        obs = self._ops([_row(name=name)]).observe_target(
            _request(lane="issue_99999_other", assigned_name=name)
        )
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_WRONG_ISSUE_LANE)

    def test_live_agent_present_is_not_stale(self):
        # a detected provider agent => live, not shell residue
        obs = self._ops([_row(agent="claude", status="idle")]).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_NOT_STALE)

    def test_working_agent_is_productive(self):
        obs = self._ops([_row(agent="claude", status="working")]).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_PRODUCTIVE)

    def test_worker_revision_mismatch_is_stale_generation(self):
        # The preflight generation gate compares the live worker ROW revision to the pinned
        # WORKER revision — NOT the lane lifecycle revision (Redmine #13806 authority split).
        obs = self._ops([_row(revision=9)]).observe_target(_request(worker_revision="3"))
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_STALE_GENERATION)

    def test_lane_revision_does_not_drive_preflight_generation(self):
        # The #13811 shape: worker row revision 0, lane lifecycle revision 5. A --lane-revision
        # that differs from the row revision must NOT trip the preflight generation gate — that
        # authority is the worker revision. So a worker row (revision 0) with worker_revision
        # pinned to "0" is actionable even while --lane-revision is a different lifecycle value.
        obs = self._ops([_row(revision=0)]).observe_target(
            _request(worker_revision="0", lane_revision="5", lane_generation="1")
        )
        self.assertEqual(decide_recovery(obs), RECOVER_ACTIONABLE)

    def test_empty_worker_revision_matches_any_present_row_revision(self):
        obs = self._ops([_row(revision=0)]).observe_target(_request(worker_revision=""))
        self.assertEqual(decide_recovery(obs), RECOVER_ACTIONABLE)


class ObserveOldSlotTests(_LiveCase):
    def _port(self, rows):
        live.list_herdr_agent_rows = lambda env: rows
        store = ReplacementTransactionStore(home=Path(tempfile.mkdtemp()))
        key = ReplacementTransactionKey(WS, "recover:k")
        return live.LiveRecoveryActuatorPort(
            repo_root=ROOT, request=_request(), store=store, key=key,
            lifecycle_home=Path(tempfile.mkdtemp()),  # isolated (empty) lane lifecycle store
        )

    def _pin(self):
        return ParticipantPin(
            lane_id=LANE, role=ROLE, provider=ROLE, assigned_name=NAME, old_locator=LOCATOR,
            lane_revision="3", lane_generation="2",
        )

    def _pin_no_lifecycle(self):
        return ParticipantPin(
            lane_id=LANE, role=ROLE, provider=ROLE, assigned_name=NAME, old_locator=LOCATOR,
        )

    def test_present(self):
        self.assertEqual(self._port([_row()]).observe_old_slot(self._pin()), OLD_SLOT_PRESENT)

    def test_absent(self):
        self.assertEqual(self._port([]).observe_old_slot(self._pin()), OLD_SLOT_ABSENT)

    def test_recycled_same_name_different_locator(self):
        self.assertEqual(
            self._port([_row(pane_id="w28:p77")]).observe_old_slot(self._pin()),
            OLD_SLOT_RECYCLED,
        )

    def test_ambiguous_multiple_exact(self):
        self.assertEqual(
            self._port([_row(), _row()]).observe_old_slot(self._pin()), OLD_SLOT_AMBIGUOUS
        )

    def test_unreadable_inventory_is_ambiguous_never_absent(self):
        def boom(env):
            raise RuntimeError("herdr down")

        live.list_herdr_agent_rows = boom
        store = ReplacementTransactionStore(home=Path(tempfile.mkdtemp()))
        port = live.LiveRecoveryActuatorPort(
            repo_root=ROOT, request=_request(), store=store,
            key=ReplacementTransactionKey(WS, "recover:k"),
        )
        self.assertEqual(port.observe_old_slot(self._pin()), OLD_SLOT_AMBIGUOUS)

    def test_preservation_running_process_blocks(self):
        obs = self._port([_row(agent="claude", status="working")]).observe_preservation(
            self._pin_no_lifecycle()
        )
        self.assertTrue(obs.running_process)

    def test_preservation_identity_match_on_exact_row(self):
        # Observable identity (lane / role / name / locator) matches; the pin carries no lane
        # lifecycle evidence here, so the lifecycle fence is not exercised.
        obs = self._port([_row()]).observe_preservation(self._pin_no_lifecycle())
        self.assertTrue(obs.identity_matches)
        self.assertFalse(obs.running_process)

    def test_preservation_lifecycle_mismatch_blocks_close(self):
        # R1-F2: a pin carrying a lane lifecycle (revision, generation) that the LIVE lane
        # lifecycle store does not back (here: no record at all) fails the identity fence — the
        # close is blocked. Never a silent pass on missing lifecycle evidence.
        obs = self._port([_row()]).observe_preservation(self._pin())  # pin has revision/gen
        self.assertFalse(obs.identity_matches)


class ActuatorDelegationTests(_LiveCase):
    """close / launch / verify delegate to the reused #13763 live ops (injected fake here)."""

    def _port(self, fake_q, *, attestation_home=None):
        store = ReplacementTransactionStore(home=Path(tempfile.mkdtemp()))
        store.plan_transaction(
            ReplacementTransactionKey(WS, "recover:k"),
            action_generation=7,
            decision=DecisionPointer(source="redmine", issue_id="13806", journal_id="79485"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13806", journal_id="79485",
                expected_gate="g", next_semantic_action="n",
            ),
            participants=[ParticipantPin(
                lane_id=LANE, role=ROLE, provider=ROLE, assigned_name=NAME, old_locator=LOCATOR,
                lane_revision="3", lane_generation="2",
            )],
        )
        port = live.LiveRecoveryActuatorPort(
            repo_root=ROOT, request=_request(), store=store,
            key=ReplacementTransactionKey(WS, "recover:k"),
            attestation_home=attestation_home,
        )
        port._q = lambda: fake_q
        return port

    def _pin(self):
        return ParticipantPin(
            lane_id=LANE, role=ROLE, provider=ROLE, assigned_name=NAME, old_locator=LOCATOR,
            lane_revision="3", lane_generation="2",
        )

    def test_close_maps_closed_to_close_done(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            CloseReceiverResult, FreshReceiverVerification,
        )

        class FakeQ:
            def close_receiver(self, req, pin): return CloseReceiverResult(closed=True)
            def heal_receiver(self, req): return None
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=True, locator="w28:p88")

        self.assertEqual(self._port(FakeQ()).close_exact_generation(self._pin()), CLOSE_DONE)

    def test_close_failure_maps_to_error(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            CloseReceiverResult,
        )

        class FakeQ:
            def close_receiver(self, req, pin):
                return CloseReceiverResult(closed=False, old_absent=False, detail="close_failed")

        self.assertEqual(self._port(FakeQ()).close_exact_generation(self._pin()), CLOSE_ERROR)

    def test_launch_error_on_exception(self):
        # launch_action_bound constructs the herdr lane actuator with the replacement action id
        # (never the plain heal_receiver, which drops it). A launch failure => LAUNCH_ERROR.
        class FailingActuator:
            def __init__(self, **kwargs):
                # the recovery MUST carry the exact action id into the fresh launch
                assert kwargs.get("replacement_action_id") == "a"
            def heal_lane_column(self, worktree_path):
                raise RuntimeError("launch failed")

        orig = live.HerdrSublaneActuatorOps
        live.HerdrSublaneActuatorOps = FailingActuator
        try:
            self.assertEqual(
                self._port(object()).launch_action_bound("a", self._pin()), LAUNCH_ERROR
            )
        finally:
            live.HerdrSublaneActuatorOps = orig

    def test_launch_carries_action_id(self):
        seen = {}

        class CapturingActuator:
            def __init__(self, **kwargs):
                seen["action_id"] = kwargs.get("replacement_action_id")
            def heal_lane_column(self, worktree_path):
                return None

        orig = live.HerdrSublaneActuatorOps
        live.HerdrSublaneActuatorOps = CapturingActuator
        try:
            self.assertEqual(
                self._port(object()).launch_action_bound("act-9", self._pin()), LAUNCH_DONE
            )
        finally:
            live.HerdrSublaneActuatorOps = orig
        self.assertEqual(seen["action_id"], "act-9")

    def _seed_attestation(self, home, *, action_id):
        HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
            assigned_name=NAME, workspace_id=WS, role=ROLE, lane_id=LANE, locator="w28:p88",
            verdict="present", replacement_action_id=action_id,
        ))

    def test_verify_bound_on_exact_action_match(self):
        # R2-F2: fresh identity AND the fresh startup attestation binds THIS action -> bound.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            FreshReceiverVerification,
        )

        class OkQ:
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=True, locator="w28:p88")

        att = Path(tempfile.mkdtemp())
        self._seed_attestation(att, action_id="act-1")
        port = self._port(OkQ(), attestation_home=att)
        self.assertEqual(port.verify_attestation("act-1", self._pin()), ATTEST_BOUND)

    def test_verify_mismatch_on_different_action(self):
        # R2-F2: a fresh, attested slot whose startup bound a DIFFERENT action -> mismatch.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            FreshReceiverVerification,
        )

        class OkQ:
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=True, locator="w28:p88")

        att = Path(tempfile.mkdtemp())
        self._seed_attestation(att, action_id="other-action")
        port = self._port(OkQ(), attestation_home=att)
        self.assertEqual(port.verify_attestation("act-1", self._pin()), ATTEST_MISMATCH)

    def test_verify_pending_when_no_attestation_record(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            FreshReceiverVerification,
        )

        class OkQ:
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=True, locator="w28:p88")

        port = self._port(OkQ(), attestation_home=Path(tempfile.mkdtemp()))  # empty store
        self.assertEqual(port.verify_attestation("act-1", self._pin()), ATTEST_PENDING)

    def test_verify_pending_when_not_fresh(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            FreshReceiverVerification,
        )

        class PendingQ:
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=False, detail="not_fresh")

        att = Path(tempfile.mkdtemp())
        self._seed_attestation(att, action_id="act-1")
        port = self._port(PendingQ(), attestation_home=att)
        self.assertEqual(port.verify_attestation("act-1", self._pin()), ATTEST_PENDING)


class RedispatchLedgerTests(_LiveCase):
    """R2-F3 / R2-R2: gate_redispatched confirms ONLY the exact-marker, fresh-target,
    herdr/queue-enter, accepted, post-launch redispatch of THIS gate to the worker."""

    ISSUE, JOURNAL = "13806", "79485"
    OLD = LOCATOR  # the vanished old worker's locator (== request.locator)
    FRESH = "w28:p99"  # the freshly-relaunched worker's distinct locator
    LAUNCH_AT = "2026-07-15T12:00:00+00:00"
    AFTER = "2026-07-15T12:05:00+00:00"
    BEFORE = "2026-07-15T11:00:00+00:00"
    MARKER = f"[mozyo:handoff:source=redmine:issue={ISSUE}:journal={JOURNAL}:kind=implementation_request:to={ROLE}]"

    def _ops(self, ledger, att_home):
        # the live inventory shows the FRESH worker (distinct locator from the old request.locator)
        live.list_herdr_agent_rows = lambda env: [_row(pane_id=self.FRESH)]
        return live.LiveStaleWorkerRecoveryOps(
            repo_root=ROOT, request=_request(), ledger=ledger, attestation_home=att_home,
        )

    def _continuation(self):
        return ContinuationPointer(
            source="redmine", issue_id=self.ISSUE, journal_id=self.JOURNAL,
            expected_gate="implementation_request", next_semantic_action="dispatch_once",
        )

    def _seed_launch(self, att_home, observed_at=LAUNCH_AT):
        HerdrIdentityAttestationStore(home=att_home).upsert(IdentityAttestationRecord(
            assigned_name=NAME, workspace_id=WS, role=ROLE, lane_id=LANE, locator=self.FRESH,
            verdict="present", observed_at=observed_at, replacement_action_id="a",
        ))

    def _delivered(self, **over):
        # a full, correct herdr worker-dispatch delivery record (as the real writer projects it)
        base = dict(
            notification_marker=self.MARKER, source="redmine", issue_id=self.ISSUE,
            journal_id=self.JOURNAL, receiver=ROLE, backend="herdr", rail="queue_enter_rail",
            target=self.FRESH, status="sent", reason="ok", recorded_at=self.AFTER,
        )
        base.update(over)
        return HerdrDeliveryLedgerRecord(**base)

    def _fixture(self, **over):
        ledger = HerdrDeliveryLedger(home=Path(tempfile.mkdtemp()))
        att = Path(tempfile.mkdtemp())
        self._seed_launch(att)
        ledger.append(self._delivered(**over))
        return self._ops(ledger, att)

    def test_confirmed_when_full_exact_delivery_after_launch(self):
        ledger = HerdrDeliveryLedger(home=Path(tempfile.mkdtemp()))
        att = Path(tempfile.mkdtemp())
        self._seed_launch(att)
        ops = self._ops(ledger, att)
        self.assertFalse(ops.gate_redispatched(self._continuation()))  # nothing yet
        ledger.append(self._delivered())
        self.assertTrue(ops.gate_redispatched(self._continuation()))

    def test_confirmed_via_real_delivery_outcome_projection(self):
        # R2-R2: prove the oracle matches what the REAL writer (record_herdr_delivery on a
        # make_outcome) projects — backend/rail are derived, not hand-set.
        from mozyo_bridge.core.state.herdr_delivery_ledger import record_herdr_delivery
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            RedmineAnchor,
            make_outcome,
        )

        led_home = Path(tempfile.mkdtemp())
        att = Path(tempfile.mkdtemp())
        self._seed_launch(att)
        outcome = make_outcome(
            status="sent", reason="ok", receiver=ROLE, target=self.FRESH,
            anchor=RedmineAnchor(issue=self.ISSUE, journal=self.JOURNAL),
            mode="queue-enter", kind="implementation_request", notification_marker=self.MARKER,
            source="redmine",
            queue_enter_turn_start_observation={"observation_kind": "post_choreography_snapshot",
                                                "runtime_state": "turn_ended", "read_ok": True},
        )
        record_herdr_delivery(outcome, home=led_home)
        ops = self._ops(HerdrDeliveryLedger(home=led_home), att)
        self.assertTrue(ops.gate_redispatched(self._continuation()))

    def test_queue_enter_reason_is_not_confirmed(self):
        self.assertFalse(self._fixture(reason="queue_enter").gate_redispatched(self._continuation()))

    def test_wrong_marker_kind_is_not_confirmed(self):
        bad = self.MARKER.replace("kind=implementation_request", "kind=custom")
        self.assertFalse(
            self._fixture(notification_marker=bad).gate_redispatched(self._continuation())
        )

    def test_continuation_gate_kind_binds_the_marker(self):
        # R3-F1: the oracle marker is built from continuation.expected_gate, so a pointer naming
        # a different gate reconstructs a different marker and never confirms the (correct
        # implementation_request) ledger record.
        ops = self._fixture()  # ledger holds the real implementation_request marker
        other = ContinuationPointer(
            source="redmine", issue_id=self.ISSUE, journal_id=self.JOURNAL,
            expected_gate="review_request", next_semantic_action="dispatch_once",
        )
        self.assertFalse(ops.gate_redispatched(other))
        self.assertTrue(ops.gate_redispatched(self._continuation()))  # the aligned pointer does

    def test_wrong_target_is_not_confirmed(self):
        self.assertFalse(self._fixture(target="w99:p1").gate_redispatched(self._continuation()))

    def test_wrong_backend_is_not_confirmed(self):
        self.assertFalse(self._fixture(backend="tmux").gate_redispatched(self._continuation()))

    def test_wrong_rail_is_not_confirmed(self):
        self.assertFalse(self._fixture(rail="event_rail").gate_redispatched(self._continuation()))

    def test_wrong_receiver_is_not_confirmed(self):
        self.assertFalse(self._fixture(receiver="codex").gate_redispatched(self._continuation()))

    def test_contradictory_provider_metadata_is_not_confirmed(self):
        # R3-F1 part2 / Design Answer j#79584: a present-but-wrong provider column is rejected.
        self.assertFalse(self._fixture(provider="codex").gate_redispatched(self._continuation()))

    def test_explicit_worker_provider_metadata_is_confirmed(self):
        # an optional provider column that DOES name the worker provider is accepted.
        self.assertTrue(self._fixture(provider=ROLE).gate_redispatched(self._continuation()))

    def test_empty_provider_metadata_is_confirmed(self):
        # the canonical real record leaves provider empty (generic writer compatibility).
        self.assertTrue(self._fixture(provider=None).gate_redispatched(self._continuation()))

    def test_delivery_before_launch_is_not_confirmed(self):
        # the same-anchor pre-recovery delivery to the old worker is temporally rejected too
        self.assertFalse(
            self._fixture(recorded_at=self.BEFORE).gate_redispatched(self._continuation())
        )

    def test_no_fresh_attestation_is_not_confirmed(self):
        ledger = HerdrDeliveryLedger(home=Path(tempfile.mkdtemp()))
        ledger.append(self._delivered())
        live.list_herdr_agent_rows = lambda env: [_row(pane_id=self.FRESH)]
        ops = live.LiveStaleWorkerRecoveryOps(
            repo_root=ROOT, request=_request(), ledger=ledger,
            attestation_home=Path(tempfile.mkdtemp()),  # empty
        )
        self.assertFalse(ops.gate_redispatched(self._continuation()))

    def test_unresolved_provider_is_not_confirmed(self):
        orig = live.resolve_worker_provider
        live.resolve_worker_provider = lambda root: (_ for _ in ()).throw(
            live.WorkflowProviderUnresolved("unbound")
        )
        try:
            self.assertFalse(self._fixture().gate_redispatched(self._continuation()))
        finally:
            live.resolve_worker_provider = orig

    def test_redispatch_gate_without_fresh_worker_is_error(self):
        # No distinct fresh worker resolved (still the old locator) => never dispatch blind.
        live.list_herdr_agent_rows = lambda env: [_row(pane_id=self.OLD)]
        ops = live.LiveStaleWorkerRecoveryOps(repo_root=ROOT, request=_request())
        self.assertEqual(ops.redispatch_gate(self._continuation()), DRAIN_SEND_ERROR)


class LaunchArgvActionIdTest(unittest.TestCase):
    """R2-F2: the wrapper argv carries --replacement-action-id ONLY for a replacement launch."""

    def _build(self, replacement_action_id):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
            build_agent_start_argv,
        )
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
            ResolvedProviderLaunch,
        )

        return build_agent_start_argv(
            assigned_name=NAME, provider=ROLE, repo_root=ROOT, workspace_id=WS, lane=LANE,
            target_workspace="wZ", target_tab="", split="", focus=False, binary="/x/herdr",
            attest_launcher="/x/mozyo-bridge", store_home="/tmp/h",
            resolved=ResolvedProviderLaunch(
                provider_id=ROLE, executable="/x/claude", managed_argv=("/x/claude",)
            ),
            launch_argv_extra=(),
            replacement_action_id=replacement_action_id,
        )

    def test_replacement_launch_emits_flag(self):
        argv = self._build("recover:xyz")
        self.assertIn("--replacement-action-id", argv)
        self.assertEqual(argv[argv.index("--replacement-action-id") + 1], "recover:xyz")
        # the capability marker (--assigned-name) is still the first wrapper flag
        self.assertLess(argv.index("--assigned-name"), argv.index("--replacement-action-id"))

    def test_normal_launch_is_byte_invariant(self):
        self.assertNotIn("--replacement-action-id", self._build(""))


def _actual_branch():
    import subprocess

    r = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
        text=True, capture_output=True,
    )
    return r.stdout.strip()


def _root_token():
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_lane_workspace_token,
    )

    return derive_lane_workspace_token(str(ROOT))


class ResumeLaneAuthorityTests(_LiveCase):
    """R3-F1 (Review j#82731 F2): the exact, effect-bound lane authority join.

    Every axis is exact: lane lifecycle (rev/gen) + canonical worktree token + a readable worktree
    on the lane's expected branch. ROOT (a real git checkout on its own branch) is the recovery
    worktree, so the positive case pins the lane to ROOT's actual branch and token.
    """

    def _declared(self, *, lane, worktree_identity):
        from mozyo_bridge.core.state.lane_lifecycle import (
            LaneLifecycleKey,
            LaneLifecycleStore,
        )

        home = Path(tempfile.mkdtemp())
        LaneLifecycleStore(home=home).declare_active(
            LaneLifecycleKey(WS, lane),
            decision=DecisionPointer(source="redmine", issue_id="13806", journal_id="79485"),
            issue_id="13806", worktree_identity=worktree_identity,
        )
        return home  # a freshly declared lane is revision 1 / generation 1

    def _ops(self, home, *, lane):
        return live.LiveStaleWorkerRecoveryOps(
            repo_root=ROOT, request=_request(lane=lane), lifecycle_home=home,
        )

    def _req(self, *, lane, rev="1", gen="1"):
        return _request(lane=lane, lane_revision=rev, lane_generation=gen)

    def test_exact_authority_all_axes_current_is_authorized(self):
        branch = _actual_branch()
        home = self._declared(lane=branch, worktree_identity=_root_token())
        self.assertTrue(self._ops(home, lane=branch).resume_lane_authority(self._req(lane=branch)))

    def test_moved_generation_is_not_authorized(self):
        branch = _actual_branch()
        home = self._declared(lane=branch, worktree_identity=_root_token())
        self.assertFalse(
            self._ops(home, lane=branch).resume_lane_authority(self._req(lane=branch, gen="2"))
        )

    def test_wrong_worktree_token_is_not_authorized(self):
        # Lifecycle rev/gen + branch match, but the canonical worktree token is a SIBLING/wrong
        # worktree's — a sibling checkout must not read as authorized (Review j#82731 F2).
        branch = _actual_branch()
        home = self._declared(lane=branch, worktree_identity="wt_deadbeefdeadbeef")
        self.assertFalse(self._ops(home, lane=branch).resume_lane_authority(self._req(lane=branch)))

    def test_empty_worktree_token_is_not_authorized(self):
        branch = _actual_branch()
        home = self._declared(lane=branch, worktree_identity="")
        self.assertFalse(self._ops(home, lane=branch).resume_lane_authority(self._req(lane=branch)))

    def test_branch_drift_is_not_authorized(self):
        # Lifecycle + token match, but the lane's expected branch is NOT ROOT's actual branch — a
        # drifted / wrong branch must not read as authorized (Review j#82731 F2).
        other = "issue_13806_some_other_branch"
        home = self._declared(lane=other, worktree_identity=_root_token())
        self.assertFalse(self._ops(home, lane=other).resume_lane_authority(self._req(lane=other)))

    def test_absent_lifecycle_is_not_authorized(self):
        home = Path(tempfile.mkdtemp())  # empty store
        branch = _actual_branch()
        self.assertFalse(self._ops(home, lane=branch).resume_lane_authority(self._req(lane=branch)))

    def test_missing_pinned_lane_evidence_is_not_authorized(self):
        branch = _actual_branch()
        home = self._declared(lane=branch, worktree_identity=_root_token())
        self.assertFalse(
            self._ops(home, lane=branch).resume_lane_authority(
                _request(lane=branch, lane_revision="", lane_generation="")
            )
        )

    def test_nonexistent_worktree_is_not_authorized(self):
        branch = _actual_branch()
        home = self._declared(lane=branch, worktree_identity=_root_token())
        ops = live.LiveStaleWorkerRecoveryOps(
            repo_root=Path("/nonexistent/mozyo_recovery_xyz"),
            request=_request(lane=branch), lifecycle_home=home,
        )
        self.assertFalse(ops.resume_lane_authority(self._req(lane=branch)))


class LaneFreeOfLiveProcessTests(_LiveCase):
    """R3-F1 (Review j#82731 F2): a pre-launch fence blocking ANY live (busy OR idle) foreign row."""

    def test_empty_inventory_is_free(self):
        self.assertTrue(self._ops([]).lane_free_of_live_process(_request()))

    def test_stale_shell_residue_at_name_is_free(self):
        # A positive shell-residue (blank agent + unknown status = SLOT_STALE) is what recover-stale
        # recovers — not a live process, so it does not fence the launch.
        self.assertTrue(self._ops([_row(agent="", status="unknown")]).lane_free_of_live_process(_request()))

    def test_busy_process_at_name_is_not_free(self):
        self.assertFalse(
            self._ops([_row(agent="claude", status="working")]).lane_free_of_live_process(_request())
        )

    def test_idle_but_live_foreign_process_at_name_is_not_free(self):
        # THE F2 gap: an IDLE but LIVE foreign row (a detected agent at a different locator) is
        # SLOT_LIVE and must block — an idle foreign process is not a safe residue.
        self.assertFalse(
            self._ops([_row(agent="codex", status="idle", pane_id="w99:p99")]).lane_free_of_live_process(
                _request()
            )
        )

    def test_unreadable_inventory_is_not_free_fail_closed(self):
        def boom(env):
            raise RuntimeError("herdr down")

        live.list_herdr_agent_rows = boom
        ops = live.LiveStaleWorkerRecoveryOps(repo_root=ROOT, request=_request())
        self.assertFalse(ops.lane_free_of_live_process(_request()))


if __name__ == "__main__":
    unittest.main()
