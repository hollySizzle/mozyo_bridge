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
    FENCE_DELIVERED,
    FENCE_RESERVED,
    FENCE_UNCERTAIN,
    FenceKey,
    dispatch_outbox_fence_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    BINDING_RESERVED,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    recovery_anchor_delivery_live as delivery_live,
    sublane_hibernated_pair_recovery_live as live,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
    REDISPATCH_FAILED,
    REDISPATCH_UNCERTAIN,
    SlotPlan,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    DETAIL_OK,
    DISPOSITION_STARTED,
    RecoveryAnchorDeliveryPreflight,
    RecoveryAnchorDeliveryOutcome,
    build_recovery_delivery_authorization_marker,
    build_recovery_delivery_zero_send_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneStartupObservation,
    SublaneStartupRoleHealth,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    SublaneHealError,
)

_WS = "wsA"
_LANE = "issue_13847_x"


def _row(name, locator, *, status="idle", cwd="/wt", revision="7"):
    return {
        "name": name,
        "pane_id": locator,
        "agent_status": status,
        "cwd": cwd,
        "revision": revision,
    }


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

    def test_v1_reserved_replacement_is_preserved_not_promoted_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            name = encode_assigned_name(_WS, "claude", _LANE)
            fake_store = SimpleNamespace(
                read=lambda action, assigned: SimpleNamespace(
                    phase=BINDING_RESERVED
                )
            )
            with patch.object(
                live, "selected_attestation_store_is_v1", return_value=True
            ), patch.object(
                live, "HerdrIdentityReplacementBindingStore",
                return_value=fake_store,
            ):
                obs, locator, an = self._observe(
                    tmp, ops, "claude", rows=[_row(name, "wZ:p3H")],
                    attested_locator="wZ:p3H",
                )
            self.assertFalse(obs.already_healthy)
            self.assertFalse(
                obs.is_bad_generation,
                "a reserved v1 launch may owe rollback and must never be closed/replayed",
            )

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

            def _deliver(self, request):
                sends.append(request)
                return RecoveryAnchorDeliveryOutcome(
                    disposition=DISPOSITION_STARTED,
                    detail=DETAIL_OK,
                )

            with patch.object(live, "list_herdr_agent_rows", return_value=[_row(gw_name, "wZ:p3G")]), \
                 patch.object(
                     live.LiveHibernatedPairRecoveryOps,
                     # Redmine #14475 (review j#88532 F1): the transport-direct checkout
                     # fence. These cases have no lifecycle row or checkout — their subject
                     # is the outbox's exactly-once behaviour — so the axis is stubbed
                     # current here and measured on its own in the #14475 suite.
                     "_checkout_authority_current", return_value=True,
                 ), \
                 patch.object(delivery_live.LiveRecoveryAnchorDeliveryService, "deliver", _deliver):
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
            self.assertEqual(sends[0].target_locator, "wZ:p3G")

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
            self.assertEqual(result, REDISPATCH_FAILED)

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
                 patch.object(
                     live.LiveHibernatedPairRecoveryOps,
                     # Redmine #14475 (review j#88532 F1): the transport-direct checkout
                     # fence. These cases have no lifecycle row or checkout — their subject
                     # is the outbox's exactly-once behaviour — so the axis is stubbed
                     # current here and measured on its own in the #14475 suite.
                     "_checkout_authority_current", return_value=True,
                 ), \
                 patch.object(delivery_live.LiveRecoveryAnchorDeliveryService, "deliver",
                              lambda self, request: sends.append(request)):
                result = ops.redispatch_to_gateway(
                    action_id="a", gateway_assigned_name=gw_name, issue="13847",
                    lane=_LANE, journal="79612", workspace_id=_WS,
                )
            self.assertEqual(result, REDISPATCH_UNCERTAIN)
            self.assertEqual(sends, [], "an un-bootstrapped fence must never send")
            # The recovery must NOT have created the fence store (no auto-bootstrap).
            self.assertFalse(fence.is_bootstrapped(), "recovery must not bootstrap the fence")

    def test_explicit_retry_preserves_prior_row_and_delivers_under_new_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            fence = DispatchOutboxFence(path=dispatch_outbox_fence_path(Path(tmp)))
            fence.bootstrap()
            approval = "88159"
            ops = _ops(tmp, fence=fence, request_journal=approval)
            gw_name = encode_assigned_name(_WS, "codex", _LANE)
            prior_action = "recover-pair:13847:issue_13847_x:3:2"
            new_action = "recovery-delivery-new"
            prior_key = FenceKey(
                workspace_id=_WS,
                lane_id=_LANE,
                issue="13847",
                journal="79612",
                action_id=prior_action,
                target_assigned_name=gw_name,
            )
            self.assertTrue(fence.reserve(prior_key).won)
            record = SimpleNamespace(
                lane_disposition=DISPOSITION_ACTIVE,
                issue_id="13847",
            )
            pair = SimpleNamespace(
                ok=True,
                gateway=SimpleNamespace(provider="codex", assigned_name=gw_name),
            )
            started = RecoveryAnchorDeliveryOutcome(
                disposition=DISPOSITION_STARTED,
                detail=DETAIL_OK,
            )
            entries = (
                RedmineJournalEntry(
                    issue_id="13847",
                    journal_id=approval,
                    notes=build_recovery_delivery_authorization_marker(
                        issue="13847",
                        lane=_LANE,
                        workspace_id=_WS,
                        anchor_journal="79612",
                        retry_of_action_id=prior_action,
                        prior_zero_send_journal="88148",
                    ),
                ),
                RedmineJournalEntry(
                    issue_id="13847",
                    journal_id="88148",
                    notes=build_recovery_delivery_zero_send_marker(
                        issue="13847",
                        lane=_LANE,
                        workspace_id=_WS,
                        anchor_journal="79612",
                        retry_of_action_id=prior_action,
                        target_assigned_name=gw_name,
                    ),
                ),
            )
            with patch.object(live, "LaneLifecycleStore",
                              return_value=SimpleNamespace(get=lambda key: record)), \
                    patch.object(
                        live.LiveHibernatedPairRecoveryOps,
                        # Redmine #14475 (review j#88532 F1): transport-direct checkout fence;
                        # not this case's subject (the stubbed record has no real checkout).
                        "_checkout_authority_current",
                        return_value=True,
                    ), \
                    patch.object(live, "read_declared_pin_pair", return_value=pair), \
                    patch.object(type(ops), "_journal_entries", return_value=entries), \
                    patch.object(live, "list_herdr_agent_rows",
                                 return_value=[_row(gw_name, "wZ:p3G")]), \
                    patch.object(
                        delivery_live.LiveRecoveryAnchorDeliveryService,
                        "deliver",
                        return_value=started,
                    ), patch.object(
                        delivery_live.LiveRecoveryAnchorDeliveryService,
                        "preflight",
                        return_value=RecoveryAnchorDeliveryPreflight(
                            may_deliver=True,
                            detail=DETAIL_OK,
                        ),
                    ):
                result = ops.retry_redispatch_to_gateway(
                    action_id=new_action,
                    retry_of_action_id=prior_action,
                    issue="13847",
                    lane=_LANE,
                    journal="79612",
                    approval_journal=approval,
                    prior_zero_send_journal="88148",
                    workspace_id=_WS,
                )
            new_key = FenceKey(
                workspace_id=_WS,
                lane_id=_LANE,
                issue="13847",
                journal="79612",
                action_id=new_action,
                target_assigned_name=gw_name,
            )
            self.assertEqual(result, REDISPATCH_DELIVERED)
            self.assertEqual(fence.state_of(prior_key), FENCE_RESERVED)
            self.assertEqual(fence.state_of(new_key), FENCE_DELIVERED)

    def test_forged_journal_ids_cannot_mint_retry_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            fence = DispatchOutboxFence(path=dispatch_outbox_fence_path(Path(tmp)))
            fence.bootstrap()
            approval = "99998"
            ops = _ops(tmp, fence=fence, request_journal=approval)
            gw_name = encode_assigned_name(_WS, "codex", _LANE)
            prior_action = "recover-pair:13847:issue_13847_x:3:2"
            prior_key = FenceKey(
                workspace_id=_WS,
                lane_id=_LANE,
                issue="13847",
                journal="79612",
                action_id=prior_action,
                target_assigned_name=gw_name,
            )
            self.assertTrue(fence.reserve(prior_key).won)
            record = SimpleNamespace(
                lane_disposition=DISPOSITION_ACTIVE,
                issue_id="13847",
            )
            pair = SimpleNamespace(
                ok=True,
                gateway=SimpleNamespace(provider="codex", assigned_name=gw_name),
            )
            authorization = build_recovery_delivery_authorization_marker(
                issue="13847",
                lane=_LANE,
                workspace_id=_WS,
                anchor_journal="79612",
                retry_of_action_id=prior_action,
                prior_zero_send_journal="99999",
            )
            with patch.object(
                type(ops),
                "_journal_entries",
                return_value=(
                    RedmineJournalEntry(
                        issue_id="13847",
                        journal_id=approval,
                        notes=authorization,
                    ),
                    RedmineJournalEntry(
                        issue_id="13847",
                        journal_id="99999",
                        notes="typed/send 0",
                    ),
                ),
            ), patch.object(
                live,
                "LaneLifecycleStore",
                return_value=SimpleNamespace(get=lambda key: record),
            ), patch.object(
                live, "read_declared_pin_pair", return_value=pair
            ), patch.object(
                delivery_live.LiveRecoveryAnchorDeliveryService, "deliver"
            ) as deliver:
                result = ops.retry_redispatch_to_gateway(
                    action_id="forged-new-action",
                    retry_of_action_id=prior_action,
                    issue="13847",
                    lane=_LANE,
                    journal="79612",
                    approval_journal=approval,
                    prior_zero_send_journal="99999",
                    workspace_id=_WS,
                )
            self.assertEqual(REDISPATCH_FAILED, result)
            self.assertEqual(FENCE_RESERVED, fence.state_of(prior_key))
            deliver.assert_not_called()

    def test_system_exit_after_reserve_becomes_uncertain_not_reserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            fence = DispatchOutboxFence(path=dispatch_outbox_fence_path(Path(tmp)))
            fence.bootstrap()
            ops = _ops(tmp, fence=fence)
            gw_name = encode_assigned_name(_WS, "codex", _LANE)
            action = "a"
            with patch.object(live, "list_herdr_agent_rows",
                              return_value=[_row(gw_name, "wZ:p3G")]), \
                    patch.object(
                        live.LiveHibernatedPairRecoveryOps,
                        # Redmine #14475 (review j#88532 F1): transport-direct checkout fence;
                        # not this case's subject (no lifecycle row / checkout here).
                        "_checkout_authority_current",
                        return_value=True,
                    ), \
                    patch.object(
                        delivery_live.LiveRecoveryAnchorDeliveryService,
                        "deliver",
                        side_effect=SystemExit(2),
                    ):
                result = ops.redispatch_to_gateway(
                    action_id=action,
                    gateway_assigned_name=gw_name,
                    issue="13847",
                    lane=_LANE,
                    journal="79612",
                    workspace_id=_WS,
                )
            key = FenceKey(
                workspace_id=_WS,
                lane_id=_LANE,
                issue="13847",
                journal="79612",
                action_id=action,
                target_assigned_name=gw_name,
            )
            self.assertEqual(result, REDISPATCH_UNCERTAIN)
            self.assertEqual(fence.state_of(key), FENCE_UNCERTAIN)


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

    def test_v1_relaunch_carries_exact_role_binding_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            calls = []

            class _FakeActuator:
                def __init__(self, **kw):
                    self.kw = kw

                def heal_lane_column(self, worktree_path, target_provider=None):
                    calls.append((self.kw, worktree_path, target_provider))

            slots = (
                SlotPlan(
                    role="gateway", provider="codex",
                    assigned_name=encode_assigned_name(_WS, "codex", _LANE),
                    declared_locator="wZ:pOldG", locator="", disposition="recover_absent",
                ),
                SlotPlan(
                    role="worker", provider="claude",
                    assigned_name=encode_assigned_name(_WS, "claude", _LANE),
                    declared_locator="wZ:pOldH", locator="wZ:p3H",
                    disposition="recover_bad_generation",
                ),
            )
            with patch.object(live, "selected_attestation_store_is_v1", return_value=True), \
                 patch.object(live, "HerdrSublaneActuatorOps", _FakeActuator):
                ok = ops.relaunch_pair(action_id="recover-a", slots=slots)

            self.assertTrue(ok)
            self.assertEqual([call[2] for call in calls], ["codex", "claude"])
            self.assertEqual(
                [
                    (
                        call[0]["replacement_action_id"],
                        call[0]["replacement_assigned_name"],
                        call[0]["replacement_old_locator"],
                        call[0]["replacement_target_only"],
                    )
                    for call in calls
                ],
                [
                    ("recover-a", slots[0].assigned_name, "wZ:pOldG", True),
                    ("recover-a", slots[1].assigned_name, "wZ:pOldH", True),
                ],
            )

    def test_v2_relaunch_preserves_single_unscoped_heal(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            calls = []

            class _FakeActuator:
                def __init__(self, **kw):
                    self.kw = kw

                def heal_lane_column(self, worktree_path, target_provider=None):
                    calls.append((self.kw, worktree_path, target_provider))

            slot = SlotPlan(
                role="worker", provider="claude",
                assigned_name=encode_assigned_name(_WS, "claude", _LANE),
                declared_locator="wZ:pOldH", locator="", disposition="recover_absent",
            )
            with patch.object(live, "selected_attestation_store_is_v1", return_value=False), \
                 patch.object(live, "HerdrSublaneActuatorOps", _FakeActuator):
                ok = ops.relaunch_pair(action_id="recover-a", slots=(slot,))

            self.assertTrue(ok)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(calls[0][2])
            self.assertEqual(calls[0][0]["replacement_action_id"], "recover-a")
            self.assertNotIn("replacement_assigned_name", calls[0][0])
            self.assertNotIn("replacement_old_locator", calls[0][0])
            self.assertNotIn("replacement_target_only", calls[0][0])

    def test_v1_missing_binding_context_fails_before_heal(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
            slot = SlotPlan(
                role="worker", provider="claude",
                assigned_name=encode_assigned_name(_WS, "claude", _LANE),
                declared_locator="", locator="", disposition="recover_absent",
            )
            with patch.object(live, "selected_attestation_store_is_v1", return_value=True), \
                 patch.object(live, "HerdrSublaneActuatorOps") as actuator:
                ok = ops.relaunch_pair(action_id="recover-a", slots=(slot,))

            self.assertFalse(ok)
            self.assertEqual(
                ops.relaunch_failure_reason,
                "replacement_binding_context_missing",
            )
            actuator.assert_not_called()

    def test_v1_heal_failure_preserves_reason_startup_and_rollback_debt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = _ops(tmp)
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

            class _FakeActuator:
                def __init__(self, **kw):
                    pass

                def heal_lane_column(self, worktree_path, target_provider=None):
                    raise SublaneHealError(
                        "safe fixed failure",
                        reason="replacement_binding_launch_unhealthy",
                        startup=startup,
                    )

            slot = SlotPlan(
                role="worker", provider="claude",
                assigned_name=encode_assigned_name(_WS, "claude", _LANE),
                declared_locator="wZ:pOldH", locator="",
                disposition="recover_absent",
            )
            with patch.object(
                live, "selected_attestation_store_is_v1", return_value=True
            ), patch.object(live, "HerdrSublaneActuatorOps", _FakeActuator):
                ok = ops.relaunch_pair(action_id="recover-a", slots=(slot,))

            self.assertFalse(ok)
            self.assertEqual(
                ops.relaunch_failure_reason,
                "replacement_binding_launch_unhealthy",
            )
            self.assertIs(ops.relaunch_failure_startup, startup)


if __name__ == "__main__":
    unittest.main()
