"""Redmine #13847 items 3/4/5 — hibernated exact-pair recovery orchestration (fake-driven).

Drives :class:`SublaneRecoverPairUseCase` against fake ops / store / resume, covering the
fail-closed choreography with NO real process: classify -> close only the bad generation ->
relaunch -> resume (verify + CAS) -> exactly-once redispatch. Every zero-close class blocks
without closing; a healthy slot is never closed; the redispatch is idempotent (fence).
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Tuple

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.lane_lifecycle import DISPOSITION_HIBERNATED
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_recover_pair_delivery import (  # noqa: E501
    SublaneRecoverPairDeliveryUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    BLOCK_CLOSE_FAILED,
    BLOCK_LANE_NOT_HIBERNATED,
    BLOCK_RELAUNCH_FAILED,
    BLOCK_RESUME_REFUSED,
    BLOCK_SLOT_PRESERVED,
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
    RecoverPairDeliveryRetryRequest,
    RecoverPairRequest,
    SublaneRecoverPairUseCase,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E501
    CAS_APPLIED,
    CasOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E501
    ResumeOutcome,
    ResumePreflight,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E501
    SLOT_PRESERVE_PRODUCTIVE,
    SlotRecoveryObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.pair_launch_attestation import (  # noqa: E501
    GATEWAY_ROLE,
    WORKER_ROLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneStartupObservation,
    SublaneStartupRoleHealth,
)


@dataclass
class _Pin:
    role: str
    provider: str
    assigned_name: str = ""
    locator: str = ""


@dataclass
class _Record:
    issue_id: str = "13847"
    lane_disposition: str = DISPOSITION_HIBERNATED
    revision: int = 3
    lane_generation: int = 2
    updated_at: str = "2026-07-16T00:00:00+00:00"
    declared_pins: tuple = field(
        default_factory=lambda: (
            _Pin(GATEWAY_ROLE, "codex", locator="wZ:pOldG"),
            _Pin(WORKER_ROLE, "claude", locator="wZ:pOldH"),
        )
    )


class _FakeStore:
    def __init__(self, record):
        self._record = record

    def get(self, key):
        return self._record


def _obs(**kw):
    base = dict(
        slot_absent=False, identity_resolved=True, belongs_to_pair=True, generation_not_newer=True,
        not_productive=True, no_pending_composer=True, worktree_readable=True,
        is_bad_generation=True, already_healthy=False,
    )
    base.update(kw)
    return SlotRecoveryObservation(**base)


def _absent(**kw):
    """A vanished pair slot (R1-F1): relaunch-recoverable, no live locator."""
    base = dict(slot_absent=True, generation_not_newer=True)
    base.update(kw)
    return SlotRecoveryObservation(**base)


class _FakeOps:
    def __init__(
        self, *, per_slot_obs, close_ok=True, relaunch_ok=True,
        relaunch_reason="", relaunch_startup=None,
        redispatch=REDISPATCH_DELIVERED,
    ):
        # per_slot_obs: {role: SlotRecoveryObservation}
        self._per_slot_obs = per_slot_obs
        self._close_ok = close_ok
        self._relaunch_ok = relaunch_ok
        self.relaunch_failure_reason = relaunch_reason
        self.relaunch_failure_startup = relaunch_startup
        self._redispatch = redispatch
        self.closed = []
        self.relaunched = False
        self.relaunch_slots = ()
        self.redispatched = None

    def workspace_id(self):
        return "wsA"

    def lane_worktree_binding_reason(self, *, lane, record) -> str:
        """#14475 (review j#88477 F1): the lane's canonical worktree-binding axis.

        Scripted, defaulting to the bound-and-matching axis so the pre-existing scenarios
        keep exercising what they were written for; a test that wants the #14462 shape sets
        ``_worktree_binding_reason`` to the failing token explicitly.
        """
        return getattr(self, "_worktree_binding_reason", LAUNCH_AUTHORITY_OK)

    def observe_slot(self, *, role, provider, workspace_id, lane, record):
        obs = self._per_slot_obs[role]
        # A vanished (absent) slot has no live locator; a live slot does.
        locator = "" if obs.slot_absent else ("wZ:p3G" if role == GATEWAY_ROLE else "wZ:p3H")
        return obs, locator, f"mzb1_wsA_{provider}_{lane}"

    def close_bad_slot(self, *, role, provider, assigned_name, locator, action_id):
        if not self._close_ok:
            return False
        self.closed.append((role, locator, action_id))
        return True

    def relaunch_pair(self, *, action_id, slots):
        self.relaunched = True
        self.relaunch_slots = slots
        return self._relaunch_ok

    def redispatch_to_gateway(self, **kw):
        self.redispatched = kw
        return self._redispatch

    def retry_redispatch_to_gateway(self, **kw):
        self.redispatched = kw
        return self._redispatch

    def preflight_retry_redispatch_to_gateway(self, **kw):
        return True, "ready"


class _FakeResume:
    def __init__(self, *, applied=True, transition="applied"):
        self._applied = applied
        self.ran = False
        #: What the disposition CAS did (Redmine #14475 review j#88547 F2). ``"applied"`` is a
        #: real commit, ``"already_active"`` an idempotent no-op that applies NOTHING, and
        #: ``None`` no transition at all. Callers that care whether this run APPLIED the
        #: resume — as opposed to merely not being blocked — need the difference.
        self._transition = transition

    def run(self, request, *, execute):
        self.ran = True
        pf = ResumePreflight(
            lane_hibernated=self._applied, release_settled=True, issue_not_reowned=True,
            pair_both_slots_live=self._applied, pair_attested=self._applied,
        )
        transition = None
        already_active = False
        if self._transition == "applied":
            transition = CasOutcome(applied=True, reason=CAS_APPLIED, revision=9)
        elif self._transition == "already_active":
            already_active = True
        return ResumeOutcome(
            executed=True, preflight=pf, issue=request.issue, lane=request.lane,
            transition=transition, already_active=already_active,
            detail="fake resume",
        )


def _use_case(ops, record=None, resume_applied=True, resume_transition="applied"):
    return SublaneRecoverPairUseCase(
        ops=ops,
        store=_FakeStore(record or _Record()),
        resume=_FakeResume(applied=resume_applied, transition=resume_transition),
    )


# Distinct journals (R1-F3): --journal is the owner APPROVAL; the redispatch re-sends the
# ORIGINAL implementation_request journal.
_APPROVAL_JOURNAL = "79697"
_ORIGINAL_IR_JOURNAL = "79612"
_REQ = RecoverPairRequest(
    issue="13847", lane="issue_13847_x",
    journal=_APPROVAL_JOURNAL, implementation_request_journal=_ORIGINAL_IR_JOURNAL,
)


class Preflight(unittest.TestCase):
    def test_not_hibernated_blocks(self):
        rec = _Record(lane_disposition="active")
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()})
        out = _use_case(ops, record=rec).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertIn(BLOCK_LANE_NOT_HIBERNATED, out.preflight.blocked_reasons)
        self.assertEqual(ops.closed, [], "nothing may be closed when not hibernated")

    def test_preflight_only_no_execute_actuates_nothing(self):
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()})
        out = _use_case(ops).run(_REQ, execute=False)
        self.assertFalse(out.executed)
        self.assertEqual(ops.closed, [])
        self.assertFalse(ops.relaunched)


class ZeroCloseGuards(unittest.TestCase):
    def test_productive_slot_blocks_and_closes_nothing(self):
        # Worker is productive (doing work) -> preserve, block, NEVER close either slot.
        ops = _FakeOps(per_slot_obs={
            GATEWAY_ROLE: _obs(already_healthy=True, is_bad_generation=False),
            WORKER_ROLE: _obs(not_productive=False),
        })
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertTrue(any(BLOCK_SLOT_PRESERVED in r for r in out.preflight.blocked_reasons))
        self.assertEqual(ops.closed, [], "a productive slot must never be closed")
        self.assertFalse(ops.relaunched)

    def test_newer_generation_slot_blocks_and_closes_nothing(self):
        ops = _FakeOps(per_slot_obs={
            GATEWAY_ROLE: _obs(already_healthy=True, is_bad_generation=False),
            WORKER_ROLE: _obs(generation_not_newer=False),
        })
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(ops.closed, [])


class Actuation(unittest.TestCase):
    def test_worker_only_bad_closes_only_worker_then_resume_redispatch(self):
        # Gateway healthy (adopted), worker stale -> close ONLY worker, relaunch, resume, redispatch.
        ops = _FakeOps(per_slot_obs={
            GATEWAY_ROLE: _obs(already_healthy=True, is_bad_generation=False),
            WORKER_ROLE: _obs(is_bad_generation=True),
        })
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked, msg=out.detail)
        self.assertEqual(out.closed_roles, (WORKER_ROLE,))
        self.assertEqual([c[0] for c in ops.closed], [WORKER_ROLE], "gateway must NOT be closed")
        self.assertTrue(ops.relaunched)
        self.assertEqual(len(ops.relaunch_slots), 1)
        self.assertEqual(ops.relaunch_slots[0].provider, "claude")
        self.assertEqual(ops.relaunch_slots[0].declared_locator, "wZ:pOldH")
        self.assertEqual(out.redispatch, REDISPATCH_DELIVERED)
        # Redispatch targets the gateway assigned name.
        self.assertIn("gateway_assigned_name", ops.redispatched)
        self.assertTrue(ops.redispatched["gateway_assigned_name"].endswith("codex_issue_13847_x"))

    def test_both_bad_closes_both(self):
        ops = _FakeOps(per_slot_obs={
            GATEWAY_ROLE: _obs(is_bad_generation=True),
            WORKER_ROLE: _obs(is_bad_generation=True),
        })
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked, msg=out.detail)
        self.assertEqual(set(out.closed_roles), {GATEWAY_ROLE, WORKER_ROLE})

    def test_relaunch_failure_blocks_before_resume(self):
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: _obs(is_bad_generation=True), WORKER_ROLE: _obs(is_bad_generation=True)},
            relaunch_ok=False,
        )
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.detail, BLOCK_RELAUNCH_FAILED)
        self.assertIsNone(out.resume)

    def test_relaunch_failure_preserves_reason_and_rollback_pointer(self):
        startup = SublaneStartupObservation(
            ok=False,
            action_id="startup-safe-id",
            roles=(
                SublaneStartupRoleHealth(
                    provider="claude", disposition="launched",
                    health="unhealthy", compensation="rollback_owed",
                ),
            ),
            rollback_owed=True,
        )
        ops = _FakeOps(
            per_slot_obs={
                GATEWAY_ROLE: _obs(is_bad_generation=True),
                WORKER_ROLE: _obs(is_bad_generation=True),
            },
            relaunch_ok=False,
            relaunch_reason="replacement_binding_launch_unhealthy",
            relaunch_startup=startup,
        )
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertEqual(out.detail, BLOCK_RELAUNCH_FAILED)
        self.assertEqual(
            out.relaunch_reason, "replacement_binding_launch_unhealthy"
        )
        self.assertIs(out.relaunch_startup, startup)
        self.assertEqual(
            out.rollback_pointer,
            "mozyo-bridge herdr session-rollback --action-id startup-safe-id",
        )
        payload = out.as_payload()
        self.assertEqual(
            payload["relaunch_startup"]["action_id"], "startup-safe-id"
        )
        self.assertEqual(payload["rollback_pointer"], out.rollback_pointer)

    def test_resume_refusal_blocks_and_skips_redispatch(self):
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(is_bad_generation=True), WORKER_ROLE: _obs(is_bad_generation=True)})
        out = _use_case(ops, resume_applied=False).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.detail, BLOCK_RESUME_REFUSED)
        self.assertIsNone(ops.redispatched, "redispatch must not fire when resume refused")

    def test_redispatch_idempotent_already(self):
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: _obs(already_healthy=True, is_bad_generation=False), WORKER_ROLE: _obs(is_bad_generation=True)},
            redispatch=REDISPATCH_ALREADY,
        )
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked)
        self.assertEqual(out.redispatch, REDISPATCH_ALREADY)


class Scenarios(unittest.TestCase):
    """The Implementation Request's required scenarios (item 6)."""

    def test_gateway_only_bad_closes_only_gateway(self):
        # Gateway stale, worker healthy -> close ONLY gateway (the worker half is kept).
        ops = _FakeOps(per_slot_obs={
            GATEWAY_ROLE: _obs(is_bad_generation=True),
            WORKER_ROLE: _obs(already_healthy=True, is_bad_generation=False),
        })
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked, msg=out.detail)
        self.assertEqual(out.closed_roles, (GATEWAY_ROLE,))
        self.assertEqual([c[0] for c in ops.closed], [GATEWAY_ROLE], "worker must NOT be closed")

    def test_partial_close_failure_blocks_before_relaunch(self):
        # A bad-generation close that fails is a partial close: block, never relaunch/resume.
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: _obs(is_bad_generation=True), WORKER_ROLE: _obs(is_bad_generation=True)},
            close_ok=False,
        )
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertTrue(out.detail.startswith(BLOCK_CLOSE_FAILED))
        self.assertFalse(ops.relaunched, "a failed close must not proceed to relaunch")
        self.assertIsNone(out.resume)

    def test_replay_both_already_healthy_closes_nothing_but_redispatches(self):
        # Restart/replay after a successful recovery: both slots already healthy -> no close,
        # no relaunch, resume runs (idempotent), and the fence makes the redispatch idempotent.
        ops = _FakeOps(per_slot_obs={
            GATEWAY_ROLE: _obs(already_healthy=True, is_bad_generation=False),
            WORKER_ROLE: _obs(already_healthy=True, is_bad_generation=False),
        }, redispatch=REDISPATCH_ALREADY)
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked, msg=out.detail)
        self.assertEqual(ops.closed, [], "a replay of an already-healthy pair closes nothing")
        self.assertFalse(ops.relaunched)
        self.assertEqual(out.redispatch, REDISPATCH_ALREADY)


class PartialCloseReplay(unittest.TestCase):
    """R1-F1: a partial close/relaunch must be replayable (an absent slot is relaunched)."""

    def test_replay_after_partial_close_relaunches_the_vanished_slot(self):
        # State after a prior run closed the gateway but the worker close failed: on replay the
        # gateway is now ABSENT (vanished) and the worker is still live-bad. The recovery must
        # relaunch the vanished gateway (no close) and close the still-live-bad worker -> finish.
        ops = _FakeOps(per_slot_obs={
            GATEWAY_ROLE: _absent(),          # closed in the prior run -> vanished
            WORKER_ROLE: _obs(is_bad_generation=True),  # still live-bad
        })
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked, msg=out.detail)
        # Only the still-live worker is closed; the absent gateway is NOT closed (no locator).
        self.assertEqual([c[0] for c in ops.closed], [WORKER_ROLE])
        self.assertTrue(ops.relaunched, "the vanished slot must be relaunched")
        self.assertEqual(
            [(slot.provider, slot.declared_locator) for slot in ops.relaunch_slots],
            [("codex", "wZ:pOldG"), ("claude", "wZ:pOldH")],
        )

    def test_replay_after_full_relaunch_failure_both_absent_relaunches(self):
        # A prior run closed both then relaunch failed: on replay both are absent. The recovery
        # relaunches both (no close needed) and finishes -> not stuck may_recover=false.
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: _absent(), WORKER_ROLE: _absent()})
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked, msg=out.detail)
        self.assertEqual(ops.closed, [], "absent slots need no close")
        self.assertTrue(ops.relaunched)

    def test_absent_slot_on_superseded_lane_preserves(self):
        # An absent slot whose lane generation was superseded must NOT be relaunched (preserve).
        ops = _FakeOps(per_slot_obs={
            GATEWAY_ROLE: _absent(generation_not_newer=False),
            WORKER_ROLE: _obs(is_bad_generation=True),
        })
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(ops.closed, [], "never touch a superseded lane")
        self.assertFalse(ops.relaunched)


class JournalSeparation(unittest.TestCase):
    """R1-F3: redispatch re-sends the ORIGINAL IR journal, not the owner-approval journal."""

    def test_redispatch_uses_original_ir_journal_not_approval(self):
        ops = _FakeOps(per_slot_obs={
            GATEWAY_ROLE: _obs(already_healthy=True, is_bad_generation=False),
            WORKER_ROLE: _obs(is_bad_generation=True),
        })
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked, msg=out.detail)
        self.assertEqual(
            ops.redispatched["journal"], _ORIGINAL_IR_JOURNAL,
            "the fence key + delivery anchor must be the ORIGINAL IR journal",
        )
        self.assertNotEqual(
            ops.redispatched["journal"], _APPROVAL_JOURNAL,
            "a re-approval (different --journal) must never change the redispatch fence key",
        )


class ActivePairRecoveryDelivery(unittest.TestCase):
    def _request(self, **changes):
        values = {
            "issue": "13847",
            "lane": "issue_13847_x",
            "journal": "88159",
            "implementation_request_journal": _ORIGINAL_IR_JOURNAL,
            "retry_of_action_id": "recover-pair:13847:issue_13847_x:3:2",
            "prior_zero_send_journal": "88148",
        }
        values.update(changes)
        return RecoverPairDeliveryRetryRequest(**values)

    def test_preflight_builds_new_action_without_dispatch(self):
        ops = _FakeOps(per_slot_obs={})
        out = SublaneRecoverPairDeliveryUseCase(ops=ops).run(
            self._request(), execute=False
        )
        self.assertFalse(out.is_blocked)
        self.assertFalse(out.executed)
        self.assertTrue(out.action_id.startswith("recovery-delivery-"))
        self.assertIsNone(ops.redispatched)

    def test_execute_keeps_original_anchor_and_binds_retry_evidence(self):
        ops = _FakeOps(per_slot_obs={})
        request = self._request()
        out = SublaneRecoverPairDeliveryUseCase(ops=ops).run(request, execute=True)
        self.assertFalse(out.is_blocked)
        self.assertEqual(out.redispatch, REDISPATCH_DELIVERED)
        self.assertEqual(
            ops.redispatched["journal"], request.implementation_request_journal
        )
        self.assertEqual(
            ops.redispatched["retry_of_action_id"], request.retry_of_action_id
        )
        self.assertEqual(
            ops.redispatched["prior_zero_send_journal"],
            request.prior_zero_send_journal,
        )
        self.assertEqual(ops.redispatched["action_id"], out.action_id)

    def test_missing_prior_evidence_is_zero_dispatch(self):
        ops = _FakeOps(per_slot_obs={})
        out = SublaneRecoverPairDeliveryUseCase(ops=ops).run(
            self._request(prior_zero_send_journal=""), execute=True
        )
        self.assertTrue(out.is_blocked)
        self.assertFalse(out.executed)
        self.assertIsNone(ops.redispatched)


if __name__ == "__main__":
    unittest.main()
