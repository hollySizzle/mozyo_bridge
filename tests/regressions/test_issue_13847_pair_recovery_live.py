"""Redmine #13847 items 3/4/5 — hibernated pair recovery LIVE adapter wiring.

Proves the live adapter really observes the inventory + attestation + lifecycle and drives
the real close / relaunch / fenced redispatch — a staged seam would leave the product gap
open (the #13806 tranche D R1-F1 lesson: a public entry point must be live-wired). Exercised
with a patched inventory + isolated attestation / lifecycle / fence stores and a fake herdr
dispatch — never a real managed pair.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    dispatch_outbox_fence_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.lane_lifecycle import DISPOSITION_HIBERNATED
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_hibernated_pair_recovery_live as live,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
    REDISPATCH_UNCERTAIN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

_WS = "wsA"
_LANE = "issue_13847_x"


def _row(name, locator, *, status="idle", cwd="/wt"):
    return {"name": name, "pane_id": locator, "agent_status": status, "cwd": cwd}


def _ops(tmp, **kw):
    base = dict(
        repo_root=Path(tmp) / "wt",
        request_issue="13847",
        request_lane=_LANE,
        request_journal="79612",
        env={},
        lifecycle_home=Path(tmp),
        attestation_home=Path(tmp),
    )
    base.update(kw)
    return live.LiveHibernatedPairRecoveryOps(**base)


def _rec(revision=3, disposition=DISPOSITION_HIBERNATED):
    return SimpleNamespace(revision=revision, lane_disposition=disposition, lane_generation=2)


class ObserveJoin(unittest.TestCase):
    """observe_slot joins inventory + attestation + lifecycle into the pure observation."""

    def _observe(self, tmp, ops, provider, *, rows, attested_locator=None, gen_ok=True):
        name = encode_assigned_name(_WS, provider, _LANE)
        if attested_locator is not None:
            HerdrIdentityAttestationStore(home=Path(tmp)).upsert(
                IdentityAttestationRecord(
                    assigned_name=name, workspace_id=_WS, role=provider, lane_id=_LANE,
                    locator=attested_locator, verdict=VERDICT_PRESENT,
                )
            )
        with patch.object(live, "list_herdr_agent_rows", return_value=rows), \
             patch.object(type(ops), "_no_pending_composer", return_value=True), \
             patch.object(type(ops), "_worktree_readable", return_value=True), \
             patch.object(type(ops), "_generation_not_newer", return_value=gen_ok):
            return ops.observe_slot(role="worker", provider=provider, workspace_id=_WS, lane=_LANE, record=_rec())

    def test_unattested_live_slot_is_bad_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            name = encode_assigned_name(_WS, "claude", _LANE)
            obs, locator, an = self._observe(
                tmp, ops, "claude", rows=[_row(name, "wZ:p3H")], attested_locator=None
            )
            self.assertTrue(obs.identity_resolved and obs.belongs_to_pair)
            self.assertTrue(obs.is_bad_generation, "a live-but-unattested slot is the bad gen")
            self.assertFalse(obs.already_healthy)
            self.assertEqual(locator, "wZ:p3H")

    def test_attested_locator_matched_slot_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            name = encode_assigned_name(_WS, "codex", _LANE)
            obs, locator, an = self._observe(
                tmp, ops, "codex", rows=[_row(name, "wZ:p3G")], attested_locator="wZ:p3G"
            )
            self.assertTrue(obs.already_healthy, "an attested locator-matched slot is healthy")
            self.assertFalse(obs.is_bad_generation)

    def test_absent_slot_is_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            obs, locator, an = self._observe(tmp, ops, "claude", rows=[])
            self.assertFalse(obs.identity_resolved)
            self.assertEqual(locator, "")

    def test_duplicate_name_is_ambiguous_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            name = encode_assigned_name(_WS, "claude", _LANE)
            obs, locator, an = self._observe(
                tmp, ops, "claude", rows=[_row(name, "wZ:p1"), _row(name, "wZ:p2")]
            )
            self.assertFalse(obs.identity_resolved, "a duplicate name is ambiguous, not resolved")


class GenerationFence(unittest.TestCase):
    """_generation_not_newer re-reads the live lifecycle and detects a newer generation."""

    def _gen_ok(self, tmp, *, live_rev, live_disp, pinned_rev):
        ops = _ops(tmp)
        fake_store = SimpleNamespace(
            get=lambda key: SimpleNamespace(revision=live_rev, lane_disposition=live_disp)
        )
        with patch.object(live, "LaneLifecycleStore", return_value=fake_store):
            return ops._generation_not_newer(_rec(revision=pinned_rev), _WS, _LANE)

    def test_same_revision_hibernated_is_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self._gen_ok(tmp, live_rev=3, live_disp=DISPOSITION_HIBERNATED, pinned_rev=3))

    def test_bumped_revision_is_newer_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A concurrent transition bumped the revision -> the pinned approval is stale.
            self.assertFalse(self._gen_ok(tmp, live_rev=5, live_disp=DISPOSITION_HIBERNATED, pinned_rev=3))

    def test_no_longer_hibernated_is_newer_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self._gen_ok(tmp, live_rev=3, live_disp="active", pinned_rev=3))


class RedispatchExactlyOnce(unittest.TestCase):
    """redispatch_to_gateway uses the fence as the sole exactly-once authority."""

    def test_first_call_delivers_then_replay_is_already(self):
        with tempfile.TemporaryDirectory() as tmp:
            fence = DispatchOutboxFence(path=dispatch_outbox_fence_path(Path(tmp)))
            fence.bootstrap()
            ops = _ops(tmp, fence=fence)
            gw_name = encode_assigned_name(_WS, "codex", _LANE)
            sends = []

            def _dispatch(self, **kw):
                sends.append(kw)
                return 0

            with patch.object(live, "list_herdr_agent_rows", return_value=[_row(gw_name, "wZ:p3G")]), \
                 patch.object(live.HerdrSublaneActuatorOps, "dispatch_implementation_request", _dispatch):
                first = ops.redispatch_to_gateway(
                    action_id="recover-pair:13847:issue_13847_x:3:2", gateway_assigned_name=gw_name,
                    issue="13847", lane=_LANE, journal="79612", workspace_id=_WS,
                )
                second = ops.redispatch_to_gateway(
                    action_id="recover-pair:13847:issue_13847_x:3:2", gateway_assigned_name=gw_name,
                    issue="13847", lane=_LANE, journal="79612", workspace_id=_WS,
                )
            self.assertEqual(first, REDISPATCH_DELIVERED)
            self.assertEqual(second, REDISPATCH_ALREADY)
            self.assertEqual(len(sends), 1, "the fence must permit exactly one gateway send")
            # The send targeted the live gateway locator.
            self.assertEqual(sends[0]["gateway_pane"], "wZ:p3G")

    def test_no_live_gateway_is_uncertain_never_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            fence = DispatchOutboxFence(path=dispatch_outbox_fence_path(Path(tmp)))
            fence.bootstrap()
            ops = _ops(tmp, fence=fence)
            gw_name = encode_assigned_name(_WS, "codex", _LANE)
            with patch.object(live, "list_herdr_agent_rows", return_value=[]):
                result = ops.redispatch_to_gateway(
                    action_id="a", gateway_assigned_name=gw_name, issue="13847",
                    lane=_LANE, journal="79612", workspace_id=_WS,
                )
            self.assertEqual(result, REDISPATCH_UNCERTAIN)

    def test_unbootstrapped_fence_is_uncertain_never_sends(self):
        # R1-F2: the recovery must NOT bootstrap a missing fence. An absent / never-bootstrapped
        # fence store => zero-send (uncertain), never a fresh reserve that could re-send.
        with tempfile.TemporaryDirectory() as tmp:
            fence = DispatchOutboxFence(path=dispatch_outbox_fence_path(Path(tmp)))  # NOT bootstrapped
            self.assertFalse(fence.is_bootstrapped())
            ops = _ops(tmp, fence=fence)
            gw_name = encode_assigned_name(_WS, "codex", _LANE)
            sends = []
            with patch.object(live, "list_herdr_agent_rows", return_value=[_row(gw_name, "wZ:p3G")]), \
                 patch.object(live.HerdrSublaneActuatorOps, "dispatch_implementation_request",
                              lambda self, **kw: sends.append(kw) or 0):
                result = ops.redispatch_to_gateway(
                    action_id="a", gateway_assigned_name=gw_name, issue="13847",
                    lane=_LANE, journal="79612", workspace_id=_WS,
                )
            self.assertEqual(result, REDISPATCH_UNCERTAIN)
            self.assertEqual(sends, [], "an un-bootstrapped fence must never send")
            # The recovery must NOT have created the fence store (no auto-bootstrap).
            self.assertFalse(fence.is_bootstrapped(), "recovery must not bootstrap the fence")


class AttestationReadFailClosed(unittest.TestCase):
    """R1-F4: an attestation store READ ERROR is not a positive bad-generation fact."""

    def test_read_error_returns_not_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            class _Boom:
                def read(self, name):
                    raise OSError("attestation store unreadable")
            with patch.object(live, "HerdrIdentityAttestationStore", return_value=_Boom()):
                record, readable = ops._read_attestation("mzb1_x")
            self.assertIsNone(record)
            self.assertFalse(readable, "a store read error must report not-readable")

    def test_readable_absent_record_is_readable(self):
        # A genuinely-absent record (store readable, no row) is (None, True) — the residue.
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)  # empty isolated attestation store
            record, readable = ops._read_attestation(encode_assigned_name(_WS, "claude", _LANE))
            self.assertIsNone(record)
            self.assertTrue(readable)

    def test_unreadable_attestation_slot_is_not_bad_generation(self):
        # End-to-end: a live slot whose attestation store is UNREADABLE must NOT be classified
        # bad-generation (would close on an unknowable store). It preserves (zero-close).
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            name = encode_assigned_name(_WS, "claude", _LANE)
            class _Boom:
                def read(self, n):
                    raise OSError("unreadable")
            with patch.object(live, "list_herdr_agent_rows", return_value=[_row(name, "wZ:p3H")]), \
                 patch.object(live, "HerdrIdentityAttestationStore", return_value=_Boom()), \
                 patch.object(type(ops), "_no_pending_composer", return_value=True), \
                 patch.object(type(ops), "_worktree_readable", return_value=True), \
                 patch.object(type(ops), "_generation_not_newer", return_value=True):
                obs, locator, an = ops.observe_slot(
                    role="worker", provider="claude", workspace_id=_WS, lane=_LANE, record=_rec())
            self.assertFalse(obs.is_bad_generation, "an unreadable attestation store must not read as bad-gen")
            self.assertFalse(obs.already_healthy)


class CloseAndRelaunchDelegate(unittest.TestCase):
    def test_close_bad_slot_delegates_to_quarantine_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            calls = []

            class _FakeQ:
                def close_receiver(self, request, pin):
                    calls.append((request.assigned_name, pin.locator))
                    return SimpleNamespace(closed=True, old_absent=False)

            with patch.object(type(ops), "_quarantine", return_value=_FakeQ()):
                ok = ops.close_bad_slot(
                    role="worker", provider="claude",
                    assigned_name=encode_assigned_name(_WS, "claude", _LANE),
                    locator="wZ:p3H", action_id="a",
                )
            self.assertTrue(ok)
            self.assertEqual(calls[0][1], "wZ:p3H", "close pin-matches the live locator")

    def test_close_old_absent_is_byte_preserving_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)

            class _FakeQ:
                def close_receiver(self, request, pin):
                    return SimpleNamespace(closed=False, old_absent=True)

            with patch.object(type(ops), "_quarantine", return_value=_FakeQ()):
                ok = ops.close_bad_slot(
                    role="worker", provider="claude",
                    assigned_name=encode_assigned_name(_WS, "claude", _LANE),
                    locator="wZ:p3H", action_id="a",
                )
            self.assertTrue(ok, "a positively-absent exact slot is byte-preserving, not a failure")


if __name__ == "__main__":
    unittest.main()
