"""Classical tests for the startup-clear exactly-once resume orchestrator (Redmine #13813).

Hermetic tests for
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume`).
They pin the whole #13813 verification matrix:

- **positive**: a cleared screen at the exact pinned target re-issues the original
  request exactly once — reserve=1, send=1, the gate advances to ``consumed``;
- **exactly-once**: a duplicate re-run, a concurrent caller, and a restart after an
  unresolved reserve all perform **zero additional send**;
- **zero-write dispositions**: a still-blocked screen, an unreadable pane, an unknown
  provider, an identity / generation mismatch, an ambiguous / unresolved target, and a
  gate-binding mismatch never touch the outbox fence (proven with an *exploding* fence);
- **not-resumable**: a pre-clear / terminal / in-flight gate is zero-read, zero-write
  (proven with an *exploding* read primitive);
- **fail-closed**: a raised / ack-only / not-started send is ``uncertain`` for operator
  reconcile (never auto-retried), and a corrupt / missing fence is fail-closed with no
  send;
- **ACK ≠ completion**: even a delivered re-issue never promotes workflow completion.

The fence is a real :class:`DispatchOutboxFence` under a per-test temp home (hermetic);
the send is a counting fake, so the fence semantics are proven with no live delivery.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.dispatch_outbox_fence import (  # noqa: E402
    FENCE_DELIVERED,
    FENCE_RESERVED,
    FENCE_UNCERTAIN,
    DispatchOutboxFence,
    DispatchOutboxFenceError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (  # noqa: E402
    SendOutcome,
    TURN_START_ACK_ONLY,
    TURN_START_NOT_STARTED,
    TURN_START_STARTED,
    TURN_START_TIMEOUT,
    TURN_START_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection import (  # noqa: E402
    PROJECT_AMBIGUOUS_TARGET,
    PROJECT_IDENTITY_MISMATCH,
    PROJECT_IDENTITY_UNRESOLVED,
    PROJECT_NEWER_GENERATION,
    PROJECT_OPERATOR_ACTION_REQUIRED,
    PROJECT_STALE_GENERATION,
    PROJECT_UNKNOWN_PROVIDER,
    PROJECT_UNREADABLE,
    RESOLUTION_AMBIGUOUS,
    RESOLUTION_RESOLVED,
    RESOLUTION_UNRESOLVED,
    ObservedStartupTarget,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume import (  # noqa: E402
    RESUME_DELIVERED,
    RESUME_FENCE_UNAVAILABLE,
    RESUME_NOT_CLEAR,
    RESUME_NOT_RESUMABLE,
    RESUME_SKIPPED,
    RESUME_UNCERTAIN,
    StartupResumeResult,
    fence_key_for_gate,
    resume_startup_gate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (  # noqa: E402
    STATE_CONSUMED,
    STATE_VERIFIED_CLEAR,
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
    supersede_gate,
    verify_clear_gate,
)

# Signatures from the real `claude` profile (agent_provider_profiles.yaml).
_THEME_SCREEN = (
    "Let's get started\n"
    "Choose the text style that looks best with your terminal\n"
    "> Dark mode"
)
_READY_COMPOSER = "esc to interrupt\n> \nType your message and press enter"


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


def _required(**target_overrides):
    return build_required_gate(
        gate_id="gate-1",
        action_generation=1,
        original_request=_original(),
        target=_target(**target_overrides),
        classification=_classification(),
    )


def _done_gate(**target_overrides):
    """A gate advanced to ``operator_reported_done`` (the resumable precondition)."""
    return report_operator_done(
        approve_gate(_required(**target_overrides), approval=GateApproval(source_journal="78412"))
    )


def _resolved(target=None) -> ObservedStartupTarget:
    return ObservedStartupTarget(
        resolution=RESOLUTION_RESOLVED, target=target if target is not None else _target()
    )


class _CountingSend:
    """A send seam counting invocations; returns a fixed outcome or raises."""

    def __init__(self, outcome=None, raises=None):
        self.calls = 0
        self._outcome = outcome
        self._raises = raises

    def __call__(self) -> SendOutcome:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._outcome is not None
        return self._outcome


def _exploding_send():
    def _send() -> SendOutcome:
        raise AssertionError("send seam must not be called on a zero-send path")

    return _send


def _exploding_read():
    def _read():
        raise AssertionError("read_visible must not be called on a zero-read path")

    return _read


class _ExplodingFence:
    """A fence stand-in that fails the test if any method is called."""

    def reserve(self, *a, **k):  # noqa: D401
        raise AssertionError("fence.reserve must not be called on a zero-write path")

    def mark_delivered(self, *a, **k):
        raise AssertionError("fence.mark_delivered must not be called on a zero-write path")

    def mark_uncertain(self, *a, **k):
        raise AssertionError("fence.mark_uncertain must not be called on a zero-write path")

    def record_uncertain(self, *a, **k):
        raise AssertionError("fence.record_uncertain must not be called on a zero-write path")


class _RaisingFence:
    """A fence whose reserve raises the fail-closed error (corrupt / lost store)."""

    def reserve(self, *a, **k):
        raise DispatchOutboxFenceError("simulated corrupt fence")


class _RowVanishesFence:
    """Delegates reserve to a real fence, but the authoritative row is deleted right
    before the outcome write, so ``mark_delivered`` / ``mark_uncertain`` return False
    (``rowcount == 0``) — the reviewer's reserve-then-row-missing reproduction (j#79268)."""

    def __init__(self, real: DispatchOutboxFence):
        self.real = real
        self.delivered_calls = 0

    def reserve(self, key, *, now=None):
        return self.real.reserve(key, now=now)

    def _wipe(self):
        import sqlite3

        conn = sqlite3.connect(self.real.path)
        conn.execute("DELETE FROM dispatch_outbox")
        conn.commit()
        conn.close()

    def mark_delivered(self, key, *, detail="", now=None):
        self.delivered_calls += 1
        self._wipe()
        return self.real.mark_delivered(key, detail=detail, now=now)  # -> False (row gone)

    def mark_uncertain(self, key, *, detail="", now=None):
        return self.real.mark_uncertain(key, detail=detail, now=now)

    def record_uncertain(self, key, *, detail="", now=None):
        # The real upsert re-asserts the never-send state even though the row was wiped.
        return self.real.record_uncertain(key, detail=detail, now=now)


class _RaiseOnWriteFence:
    """Reserve succeeds, but every outcome write raises the fail-closed store error."""

    def __init__(self, real: DispatchOutboxFence):
        self.real = real

    def reserve(self, key, *, now=None):
        return self.real.reserve(key, now=now)

    def mark_delivered(self, key, *, detail="", now=None):
        raise DispatchOutboxFenceError("store corrupt at outcome write")

    def mark_uncertain(self, key, *, detail="", now=None):
        raise DispatchOutboxFenceError("store corrupt at outcome write")

    def record_uncertain(self, key, *, detail="", now=None):
        raise DispatchOutboxFenceError("store corrupt at outcome write")


class _ResumeCase(unittest.TestCase):
    """Base with a real hermetic fence under a temp home."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.fence = DispatchOutboxFence(home=self.home)
        self.fence.bootstrap()

    def _resume(self, *, gate, observed, read_visible, fence, send, observed_at="2026-07-16T01:00:00Z"):
        return resume_startup_gate(
            existing_gate=gate,
            observed=observed,
            read_visible=read_visible,
            fence=fence,
            send=send,
            profile_version="2",
            classifier_version="1",
            observed_at=observed_at,
        )


class PositiveResumeTests(_ResumeCase):
    def test_clear_screen_reissues_exactly_once(self) -> None:
        send = _CountingSend(outcome=SendOutcome(turn_start=TURN_START_STARTED))
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=send,
        )
        self.assertEqual(result.result, RESUME_DELIVERED)
        self.assertEqual(send.calls, 1)  # send=1
        self.assertTrue(result.sent)
        self.assertTrue(result.reserved)  # reserve=1
        self.assertEqual(result.fence_state, FENCE_DELIVERED)
        self.assertIsNotNone(result.advanced_gate)
        assert result.advanced_gate is not None
        self.assertEqual(result.advanced_gate.state, STATE_CONSUMED)
        self.assertEqual(
            result.advanced_gate.resume.consumed_delivery_record, "deliv-1"
        )
        # The fence really recorded a delivered row for the key.
        self.assertEqual(self.fence.state_of(fence_key_for_gate(_done_gate())), FENCE_DELIVERED)

    def test_delivered_is_not_a_completion(self) -> None:
        send = _CountingSend(outcome=SendOutcome(turn_start=TURN_START_STARTED))
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=send,
        )
        self.assertEqual(result.result, RESUME_DELIVERED)
        # ACK / delivery / completion separation: a delivered re-issue never promotes.
        self.assertFalse(result.promotes_workflow_completion)


class ExactlyOnceTests(_ResumeCase):
    def test_duplicate_rerun_never_sends_again(self) -> None:
        first = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=_CountingSend(outcome=SendOutcome(turn_start=TURN_START_STARTED)),
        )
        self.assertEqual(first.result, RESUME_DELIVERED)
        # A second, identical resume must perform ZERO send.
        second = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=_exploding_send(),
        )
        self.assertEqual(second.result, RESUME_SKIPPED)
        self.assertFalse(second.sent)
        self.assertEqual(second.fence_state, FENCE_DELIVERED)

    def test_concurrent_second_caller_skips(self) -> None:
        # First caller wins the reserve and sends; the second (same key) never sends.
        self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=_CountingSend(outcome=SendOutcome(turn_start=TURN_START_STARTED)),
        )
        second = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=_exploding_send(),
        )
        self.assertEqual(second.result, RESUME_SKIPPED)
        self.assertFalse(second.sent)

    def test_restart_after_unresolved_reserve_is_reconcile_not_retry(self) -> None:
        # Simulate a crash: a prior run reserved the key but never resolved the outcome.
        key = fence_key_for_gate(_done_gate())
        won = self.fence.reserve(key)
        self.assertTrue(won.won)
        # The restart must NOT blindly retry: the still-reserved key surfaces uncertain.
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=_exploding_send(),
        )
        self.assertEqual(result.result, RESUME_SKIPPED)
        self.assertFalse(result.sent)
        self.assertTrue(result.needs_reconcile)
        self.assertEqual(result.fence_state, FENCE_UNCERTAIN)


class NotClearZeroWriteTests(_ResumeCase):
    """A non-clear projection disposition -> zero send AND zero fence touch."""

    def _assert_not_clear(self, *, observed, read_visible, disposition) -> StartupResumeResult:
        result = self._resume(
            gate=_done_gate(),
            observed=observed,
            read_visible=read_visible,
            fence=_ExplodingFence(),  # any fence touch fails the test
            send=_exploding_send(),
        )
        self.assertEqual(result.result, RESUME_NOT_CLEAR)
        self.assertFalse(result.sent)
        self.assertFalse(result.reserved)
        self.assertEqual(result.projection_disposition, disposition)
        return result

    def test_still_blocked(self) -> None:
        self._assert_not_clear(
            observed=_resolved(),
            read_visible=lambda: _THEME_SCREEN,
            disposition=PROJECT_OPERATOR_ACTION_REQUIRED,
        )

    def test_unreadable(self) -> None:
        def _raises():
            raise RuntimeError("transport down")

        self._assert_not_clear(
            observed=_resolved(), read_visible=_raises, disposition=PROJECT_UNREADABLE
        )

    def test_unknown_provider(self) -> None:
        # The gate AND the live target are on the same unprofiled provider (so identity
        # matches and the classifier — not stale判定 — is what fails closed).
        result = self._resume(
            gate=_done_gate(provider_id="ghostprovider"),
            observed=_resolved(_target(provider_id="ghostprovider")),
            read_visible=lambda: "x",
            fence=_ExplodingFence(),
            send=_exploding_send(),
        )
        self.assertEqual(result.result, RESUME_NOT_CLEAR)
        self.assertEqual(result.projection_disposition, PROJECT_UNKNOWN_PROVIDER)

    def test_identity_mismatch_zero_read(self) -> None:
        # The gate is pinned to lane-alpha; the live target is lane-beta. Stale判定
        # short-circuits BEFORE the pane read -> exploding read must never run.
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(_target(lane_id="lane-beta")),
            read_visible=_exploding_read(),
            fence=_ExplodingFence(),
            send=_exploding_send(),
        )
        self.assertEqual(result.result, RESUME_NOT_CLEAR)
        self.assertEqual(result.projection_disposition, PROJECT_IDENTITY_MISMATCH)

    def test_newer_generation_zero_read(self) -> None:
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(_target(agent_generation=9)),
            read_visible=_exploding_read(),
            fence=_ExplodingFence(),
            send=_exploding_send(),
        )
        self.assertEqual(result.projection_disposition, PROJECT_NEWER_GENERATION)

    def test_stale_generation_zero_read(self) -> None:
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(_target(agent_generation=1)),
            read_visible=_exploding_read(),
            fence=_ExplodingFence(),
            send=_exploding_send(),
        )
        self.assertEqual(result.projection_disposition, PROJECT_STALE_GENERATION)

    def test_ambiguous_target_zero_read(self) -> None:
        result = self._resume(
            gate=_done_gate(),
            observed=ObservedStartupTarget(resolution=RESOLUTION_AMBIGUOUS),
            read_visible=_exploding_read(),
            fence=_ExplodingFence(),
            send=_exploding_send(),
        )
        self.assertEqual(result.projection_disposition, PROJECT_AMBIGUOUS_TARGET)

    def test_unresolved_target_zero_read(self) -> None:
        result = self._resume(
            gate=_done_gate(),
            observed=ObservedStartupTarget(resolution=RESOLUTION_UNRESOLVED),
            read_visible=_exploding_read(),
            fence=_ExplodingFence(),
            send=_exploding_send(),
        )
        self.assertEqual(result.projection_disposition, PROJECT_IDENTITY_UNRESOLVED)


class NotResumableZeroReadTests(_ResumeCase):
    """A pre-clear / terminal / in-flight gate is zero-read, zero-write."""

    def _assert_not_resumable(self, gate) -> StartupResumeResult:
        result = self._resume(
            gate=gate,
            observed=_resolved(),
            read_visible=_exploding_read(),  # the pane is never read
            fence=_ExplodingFence(),
            send=_exploding_send(),
        )
        self.assertEqual(result.result, RESUME_NOT_RESUMABLE)
        self.assertFalse(result.sent)
        self.assertFalse(result.reserved)
        return result

    def test_required_is_pre_clear(self) -> None:
        self._assert_not_resumable(_required())

    def test_owner_approved_is_pre_clear(self) -> None:
        self._assert_not_resumable(
            approve_gate(_required(), approval=GateApproval(source_journal="78412"))
        )

    def test_consumed_is_terminal(self) -> None:
        cleared = verify_clear_gate(
            _done_gate(),
            startup_clear_observed_at="2026-07-16T01:00:00Z",
            dispatch_fence_state=FENCE_RESERVED,
        )
        consumed = consume_gate(cleared, consumed_delivery_record="deliv-1")
        self._assert_not_resumable(consumed)

    def test_superseded_is_terminal(self) -> None:
        self._assert_not_resumable(supersede_gate(_done_gate()))

    def test_verified_clear_uncertain_flags_reconcile(self) -> None:
        in_flight = verify_clear_gate(
            _done_gate(),
            startup_clear_observed_at="2026-07-16T01:00:00Z",
            dispatch_fence_state=FENCE_UNCERTAIN,
        )
        result = self._assert_not_resumable(in_flight)
        self.assertTrue(result.needs_reconcile)


class UncertainFailClosedTests(_ResumeCase):
    """Reserve won + send outcome unknown -> uncertain, reconcile, gate verified_clear."""

    def _assert_uncertain(self, send) -> StartupResumeResult:
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=send,
        )
        self.assertEqual(result.result, RESUME_UNCERTAIN)
        self.assertTrue(result.sent)
        self.assertTrue(result.reserved)
        self.assertTrue(result.needs_reconcile)
        self.assertEqual(result.fence_state, FENCE_UNCERTAIN)
        assert result.advanced_gate is not None
        self.assertEqual(result.advanced_gate.state, STATE_VERIFIED_CLEAR)
        # The fence recorded uncertain (never delivered) -> a later run skips.
        self.assertEqual(
            self.fence.state_of(fence_key_for_gate(_done_gate())), FENCE_UNCERTAIN
        )
        return result

    def test_send_raises(self) -> None:
        send = _CountingSend(raises=RuntimeError("boom"))
        self._assert_uncertain(send)
        self.assertEqual(send.calls, 1)

    def test_ack_only(self) -> None:
        self._assert_uncertain(_CountingSend(outcome=SendOutcome(turn_start=TURN_START_ACK_ONLY)))

    def test_not_started(self) -> None:
        self._assert_uncertain(_CountingSend(outcome=SendOutcome(turn_start=TURN_START_NOT_STARTED)))

    def test_timeout(self) -> None:
        self._assert_uncertain(_CountingSend(outcome=SendOutcome(turn_start=TURN_START_TIMEOUT)))

    def test_unknown(self) -> None:
        self._assert_uncertain(_CountingSend(outcome=SendOutcome(turn_start=TURN_START_UNKNOWN)))

    def test_uncertain_then_rerun_skips(self) -> None:
        self._assert_uncertain(_CountingSend(outcome=SendOutcome(turn_start=TURN_START_ACK_ONLY)))
        rerun = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=_exploding_send(),
        )
        self.assertEqual(rerun.result, RESUME_SKIPPED)
        self.assertFalse(rerun.sent)
        self.assertTrue(rerun.needs_reconcile)


class PostReserveOutcomeWriteFailClosedTests(_ResumeCase):
    """Finding 2 (j#79268): an unconfirmable post-reserve outcome write must NOT report
    delivered/consumed — it fails closed to uncertain so a re-run performs zero send."""

    def test_delivered_write_row_missing_fails_closed_to_uncertain(self) -> None:
        wrapper = _RowVanishesFence(self.fence)
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=wrapper,
            send=_CountingSend(outcome=SendOutcome(turn_start=TURN_START_STARTED)),
        )
        # The send's turn-start was confirmed, but the fence could not durably record it.
        self.assertEqual(result.result, RESUME_UNCERTAIN)
        self.assertNotEqual(result.result, RESUME_DELIVERED)
        self.assertTrue(result.needs_reconcile)
        self.assertEqual(result.fence_state, FENCE_UNCERTAIN)
        assert result.advanced_gate is not None
        self.assertEqual(result.advanced_gate.state, STATE_VERIFIED_CLEAR)
        self.assertNotEqual(result.advanced_gate.state, STATE_CONSUMED)
        self.assertEqual(wrapper.delivered_calls, 1)

    def test_delivered_write_raises_fails_closed_without_propagating(self) -> None:
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=_RaiseOnWriteFence(self.fence),
            send=_CountingSend(outcome=SendOutcome(turn_start=TURN_START_STARTED)),
        )
        # A raised outcome write must not propagate out of a path that already sent once.
        self.assertEqual(result.result, RESUME_UNCERTAIN)
        self.assertTrue(result.needs_reconcile)
        assert result.advanced_gate is not None
        self.assertEqual(result.advanced_gate.state, STATE_VERIFIED_CLEAR)

    def test_uncertain_write_raises_does_not_propagate(self) -> None:
        # A non-started send whose uncertain write also raises must still return uncertain.
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=_RaiseOnWriteFence(self.fence),
            send=_CountingSend(outcome=SendOutcome(turn_start=TURN_START_ACK_ONLY)),
        )
        self.assertEqual(result.result, RESUME_UNCERTAIN)
        self.assertTrue(result.needs_reconcile)

    def test_rerun_reading_stale_gate_after_row_loss_sends_zero(self) -> None:
        # Review j#79309 Finding 1: exactly-once must hold at the FENCE, not the gate. Run 1
        # loses the reserved row at the outcome write and returns uncertain; the advanced
        # `verified_clear` gate is NEVER durably recorded. Run 2 re-reads the STALE durable
        # gate (still `operator_reported_done`) against the SAME real fence — and must send
        # zero because record_uncertain re-asserted the never-send state on the fence.
        send = _CountingSend(outcome=SendOutcome(turn_start=TURN_START_STARTED))
        first = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=_RowVanishesFence(self.fence),
            send=send,
        )
        self.assertEqual(first.result, RESUME_UNCERTAIN)
        self.assertEqual(send.calls, 1)
        # The fence itself now holds the never-send uncertain state (re-asserted upsert).
        self.assertEqual(
            self.fence.state_of(fence_key_for_gate(_done_gate())), FENCE_UNCERTAIN
        )
        # Re-run with the STALE gate (recording of verified_clear is assumed to have failed).
        rerun = self._resume(
            gate=_done_gate(),  # stale durable pointer, NOT first.advanced_gate
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=_exploding_send(),
        )
        self.assertEqual(rerun.result, RESUME_SKIPPED)
        self.assertFalse(rerun.sent)
        self.assertTrue(rerun.needs_reconcile)
        self.assertEqual(send.calls, 1)  # still exactly one send total across both runs

    def test_rerun_after_whole_store_loss_fails_closed(self) -> None:
        # If the WHOLE store is lost (not just a row), record_uncertain can't re-assert, but
        # a re-run then fails closed on the fence's _connect (no send).
        send = _CountingSend(outcome=SendOutcome(turn_start=TURN_START_STARTED))
        first = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=_RaiseOnWriteFence(self.fence),
            send=send,
        )
        self.assertEqual(first.result, RESUME_UNCERTAIN)
        self.assertEqual(send.calls, 1)
        # Simulate the whole store lost after the send.
        self.fence.path.unlink()
        rerun = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=_exploding_send(),
        )
        self.assertEqual(rerun.result, RESUME_FENCE_UNAVAILABLE)
        self.assertEqual(send.calls, 1)


class FenceUnavailableTests(_ResumeCase):
    def test_unbootstrapped_fence_fails_closed_no_send(self) -> None:
        # A fresh (never bootstrapped) fence must fail closed with no send.
        with tempfile.TemporaryDirectory() as d:
            fence = DispatchOutboxFence(home=Path(d))  # not bootstrapped
            result = self._resume(
                gate=_done_gate(),
                observed=_resolved(),
                read_visible=lambda: _READY_COMPOSER,
                fence=fence,
                send=_exploding_send(),
            )
        self.assertEqual(result.result, RESUME_FENCE_UNAVAILABLE)
        self.assertFalse(result.sent)

    def test_raising_fence_fails_closed_no_send(self) -> None:
        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=_RaisingFence(),
            send=_exploding_send(),
        )
        self.assertEqual(result.result, RESUME_FENCE_UNAVAILABLE)
        self.assertFalse(result.sent)


class RecordSafetyTests(_ResumeCase):
    def test_advanced_gate_record_is_path_and_secret_safe(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate_lattice import (
            operator_startup_resume_record_lines,
        )

        result = self._resume(
            gate=_done_gate(),
            observed=_resolved(),
            read_visible=lambda: _READY_COMPOSER,
            fence=self.fence,
            send=_CountingSend(outcome=SendOutcome(turn_start=TURN_START_STARTED)),
        )
        assert result.advanced_gate is not None
        for line in operator_startup_resume_record_lines(result.advanced_gate):
            self.assertNotIn("/Users/", line)
            self.assertNotIn("api_key", line)
            self.assertNotIn("password", line)


class ResultContractTests(unittest.TestCase):
    def test_unknown_result_token_rejected(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume import (
            StartupResumeError,
        )

        with self.assertRaises(StartupResumeError):
            StartupResumeResult(result="teleported")


if __name__ == "__main__":
    unittest.main()
