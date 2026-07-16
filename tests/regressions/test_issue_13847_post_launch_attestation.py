"""Regression pins for Redmine #13847 — post-launch pair self-attestation gate (item 1).

A launch that returns a live locator is not proof the pair attested. `sublane create/start`
must confirm BOTH slots' #13637 startup self-attestation at action time and refuse to
promote a partial / unattested / stale pair to `executed` — instead returning a typed
`partial_pair_recovery_required` blocker with a durable recovery pointer (the live evidence
was `sublane create --no-dispatch --execute` returning `executed` for a pair that then read
gateway=`unattested` / worker=`stale_named_slot`).

Three surfaces, matching the probe / decision / orchestration / adapter split:

- the pure decision `decide_pair_launch_attestation` (both-ok vs partial, naming bad roles);
- the create/start gate `pair_attestation_admission` (fresh-launch only, bounded poll,
  fail-closed block, no dispatch, recovery pointer; adopt is skipped);
- the live observation helper `observe_lane_pair_attestation` (read + join per slot; absent
  / stale / unobserved all fail closed).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.herdr_identity_attestation import (
    ATTEST_OK,
    ATTEST_STALE,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
    SublaneActuateUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_pair_attestation_ops import (  # noqa: E501
    observe_lane_pair_attestation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.pair_launch_attestation import (  # noqa: E501
    GATEWAY_ROLE,
    PAIR_ATTESTED,
    PARTIAL_PAIR_RECOVERY_REQUIRED,
    WORKER_ROLE,
    SlotAttestation,
    decide_pair_launch_attestation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    REASON_PARTIAL_PAIR_RECOVERY,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

from tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_sublane_actuator import (  # noqa: E501
    FakeActuatorOps,
    _lane,
    _req,
)


def _slot(role, *, ok, state="attested", locator="wZ:p2"):
    return SlotAttestation(
        role=role, assigned_name=f"mzb1_ws_{role}", ok=ok, state=state, locator=locator
    )


def _ok_pair():
    return (_slot(GATEWAY_ROLE, ok=True), _slot(WORKER_ROLE, ok=True))


class _AttestingOps(FakeActuatorOps):
    """FakeActuatorOps that also answers the #13847 `observe_pair_attestation` port."""

    def __init__(self, *, pair_script, **kw):
        super().__init__(**kw)
        # A list of (gateway_slot, worker_slot) tuples consumed one per probe (last sticky).
        self._pair_script = list(pair_script)

    def observe_pair_attestation(self, worktree_path):
        self.calls.append(("observe_pair_attestation", worktree_path))
        if len(self._pair_script) > 1:
            return self._pair_script.pop(0)
        return self._pair_script[0]


def _use_case(ops):
    # No-op sleep + a small probe bound so the bounded poll never really waits.
    return SublaneActuateUseCase(ops, gateway_ready_probes=3, sleep=lambda _s: None)


class PureDecision(unittest.TestCase):
    def test_both_ok_is_attested(self):
        v = decide_pair_launch_attestation(*_ok_pair())
        self.assertTrue(v.ok)
        self.assertEqual(v.reason, PAIR_ATTESTED)
        self.assertEqual(v.blocked_roles, ())

    def test_gateway_unattested_is_partial(self):
        v = decide_pair_launch_attestation(
            _slot(GATEWAY_ROLE, ok=False, state="unattested"),
            _slot(WORKER_ROLE, ok=True),
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, PARTIAL_PAIR_RECOVERY_REQUIRED)
        self.assertEqual(v.blocked_roles, (GATEWAY_ROLE,))
        self.assertIn("gateway=unattested", v.blocked_summary())

    def test_both_bad_lists_both_gateway_first(self):
        v = decide_pair_launch_attestation(
            _slot(GATEWAY_ROLE, ok=False, state="stale"),
            _slot(WORKER_ROLE, ok=False, state="absent"),
        )
        self.assertEqual(v.blocked_roles, (GATEWAY_ROLE, WORKER_ROLE))

    def test_guard_bite_a_worker_only_failure_still_blocks(self):
        # Adversarial: if the decision required BOTH to fail (an AND bug), a half-booted
        # pair would pass. A single bad slot must fail closed.
        v = decide_pair_launch_attestation(
            _slot(GATEWAY_ROLE, ok=True),
            _slot(WORKER_ROLE, ok=False, state="stale_named_slot"),
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.blocked_roles, (WORKER_ROLE,))


class CreateStartGate(unittest.TestCase):
    def test_attested_pair_reaches_executed(self):
        ops = _AttestingOps(git=True, lanes=[None, _lane()], pair_script=[_ok_pair()])
        outcome = _use_case(ops).run(_req(), execute=True, dispatch=False)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertIn(("observe_pair_attestation", "/wt/12973"), ops.calls)

    def test_unattested_pair_blocks_partial_and_never_dispatches(self):
        # The exact live evidence: create --no-dispatch --execute, worker stale.
        script = [(_slot(GATEWAY_ROLE, ok=True), _slot(WORKER_ROLE, ok=False, state="stale_named_slot"))]
        ops = _AttestingOps(git=True, lanes=[None, _lane()], pair_script=script)
        outcome = _use_case(ops).run(_req(), execute=True, dispatch=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_PARTIAL_PAIR_RECOVERY, outcome.blocked_reasons)
        self.assertIn("unattested:worker", outcome.blocked_reasons)
        self.assertNotIn("dispatch", ops._names())
        # A durable recovery pointer (the public exact-pair recovery command) is surfaced.
        recover_steps = [s for s in outcome.steps if s.command and "recover-pair" in s.command]
        self.assertTrue(recover_steps, "a recover-pair recovery command must be surfaced")

    def test_bounded_poll_succeeds_when_attestation_lands_late(self):
        # First probe: worker not yet attested; second probe: both attested.
        script = [
            (_slot(GATEWAY_ROLE, ok=True), _slot(WORKER_ROLE, ok=False, state="absent")),
            _ok_pair(),
        ]
        ops = _AttestingOps(git=True, lanes=[None, _lane()], pair_script=script)
        outcome = _use_case(ops).run(_req(), execute=True, dispatch=False)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)

    def test_adopt_is_not_gated_on_fresh_attestation(self):
        # An adopt of an already-live pair (both panes present on the FIRST read) is
        # validated by the owner-declaration gate; re-requiring fresh attestation would
        # wrongly block a healthy adopt. The gate must be skipped -> executed even though
        # the (irrelevant) script would report unattested.
        script = [(_slot(GATEWAY_ROLE, ok=False, state="stale"), _slot(WORKER_ROLE, ok=False, state="stale"))]
        ops = _AttestingOps(
            git=True, lanes=[_lane(), _lane()], pair_script=script
        )
        outcome = _use_case(ops).run(_req(), execute=True, dispatch=False)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertNotIn(
            ("observe_pair_attestation", "/wt/12973"),
            ops.calls,
            "adopt must not invoke the fresh-launch attestation gate",
        )


class LiveObservationHelper(unittest.TestCase):
    """`observe_lane_pair_attestation` joins each slot's record against the live locator."""

    def _store(self, tmp):
        return HerdrIdentityAttestationStore(path=Path(tmp) / "attest.sqlite")

    def _write(self, store, *, ws, provider, lane, locator):
        store.upsert(
            IdentityAttestationRecord(
                assigned_name=encode_assigned_name(ws, provider, lane),
                workspace_id=ws,
                role=provider,
                lane_id=lane,
                locator=locator,
                verdict=VERDICT_PRESENT,
            )
        )

    def _resolver(self, ws, lane, slots):
        def resolve(worktree_path, rows, managed):
            return ws, lane, slots

        return resolve

    def test_both_present_and_locator_matched_are_ok(self):
        ws, lane = "wsA", "issue_x"
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._write(store, ws=ws, provider="codex", lane=lane, locator="wZ:p2")
            self._write(store, ws=ws, provider="claude", lane=lane, locator="wZ:p3")
            slots = {"codex": ("wZ:p2", "k"), "claude": ("wZ:p3", "k")}
            gw, wk = observe_lane_pair_attestation(
                worktree_path="/wt",
                gateway_provider="codex",
                worker_provider="claude",
                list_rows=lambda: [],
                resolve_slots=self._resolver(ws, lane, slots),
                attestation_store=store,
            )
            self.assertTrue(gw.ok and wk.ok)
            self.assertEqual(gw.state, ATTEST_OK)
            self.assertEqual(gw.role, GATEWAY_ROLE)
            self.assertEqual(wk.role, WORKER_ROLE)

    def test_stale_locator_fails_closed(self):
        # Record's locator no longer matches the live locator -> a different generation.
        ws, lane = "wsA", "issue_x"
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._write(store, ws=ws, provider="codex", lane=lane, locator="OLD:p2")
            self._write(store, ws=ws, provider="claude", lane=lane, locator="wZ:p3")
            slots = {"codex": ("wZ:p2", "k"), "claude": ("wZ:p3", "k")}
            gw, wk = observe_lane_pair_attestation(
                worktree_path="/wt",
                gateway_provider="codex",
                worker_provider="claude",
                list_rows=lambda: [],
                resolve_slots=self._resolver(ws, lane, slots),
                attestation_store=store,
            )
            self.assertFalse(gw.ok)
            self.assertEqual(gw.state, ATTEST_STALE)
            self.assertTrue(wk.ok)

    def test_absent_record_and_unobserved_slot_fail_closed(self):
        ws, lane = "wsA", "issue_x"
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            # Only the gateway attested; worker slot is not even in the inventory.
            self._write(store, ws=ws, provider="codex", lane=lane, locator="wZ:p2")
            slots = {"codex": ("wZ:p2", "k")}  # worker missing -> unobserved
            gw, wk = observe_lane_pair_attestation(
                worktree_path="/wt",
                gateway_provider="codex",
                worker_provider="claude",
                list_rows=lambda: [],
                resolve_slots=self._resolver(ws, lane, slots),
                attestation_store=store,
            )
            self.assertTrue(gw.ok)
            self.assertFalse(wk.ok)
            self.assertEqual(wk.state, "unobserved")

    def test_unresolved_lane_unit_fails_closed_both(self):
        gw, wk = observe_lane_pair_attestation(
            worktree_path="/wt",
            gateway_provider="codex",
            worker_provider="claude",
            list_rows=lambda: [],
            resolve_slots=self._resolver("", "", {}),
        )
        self.assertFalse(gw.ok or wk.ok)


if __name__ == "__main__":
    unittest.main()
