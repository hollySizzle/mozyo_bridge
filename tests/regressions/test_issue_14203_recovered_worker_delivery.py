"""R18: recovered pair worker delivery keeps lifecycle and work authority separate."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.core.state.lane_lifecycle import DISPOSITION_ACTIVE
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    recovered_worker_delivery_live as worker_delivery_live,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovered_worker_delivery import (  # noqa: E501
    RecoveredWorkerDeliveryUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    REDISPATCH_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_worker_delivery import (  # noqa: E501
    RecoveredWorkerDeliveryRequest,
    recovered_worker_forward_attempt_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    build_recovery_delivery_authorization_marker,
    build_recovery_delivery_zero_send_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)


ISSUE = "14203"
LANE = "issue_14203_gateway_recovery_r18"
WORKSPACE = "ws-r18"
WORK_ANCHOR = "88143"
LIFECYCLE_DECISION = "88145"
TARGET_ACTION = "recover-pair:14203:r18:5:1"
WORKER_NAME = "mzb1_ws-r18_claude_issueZ5F14203Z5FgatewayZ5FrecoveryZ5Fr18"


def retry_of() -> str:
    return recovered_worker_forward_attempt_id(
        issue=ISSUE,
        lane=LANE,
        workspace_id=WORKSPACE,
        lane_generation=1,
        lifecycle_decision_journal=LIFECYCLE_DECISION,
        anchor_journal=WORK_ANCHOR,
        target_action_id=TARGET_ACTION,
        target_assigned_name=WORKER_NAME,
    )


def request(**changes: str) -> RecoveredWorkerDeliveryRequest:
    values = {
        "issue": ISSUE,
        "lane": LANE,
        "journal": "88290",
        "implementation_request_journal": WORK_ANCHOR,
        "lifecycle_decision_journal": LIFECYCLE_DECISION,
        "target_action_id": TARGET_ACTION,
        "retry_of_action_id": retry_of(),
        "prior_zero_send_journal": "88291",
    }
    values.update(changes)
    return RecoveredWorkerDeliveryRequest(**values)


class FakeOps:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.preflight = None
        self.delivered = None

    def workspace_id(self) -> str:
        return WORKSPACE

    def preflight_retry_redispatch_to_worker(self, **kwargs):
        self.preflight = kwargs
        return self.ready, "ready" if self.ready else "lifecycle_decision_moved"

    def retry_redispatch_to_worker(self, **kwargs):
        self.delivered = kwargs
        return REDISPATCH_DELIVERED


class RecoveredWorkerDeliveryUseCaseTest(unittest.TestCase):
    def test_preflight_preserves_distinct_lifecycle_and_work_anchors(self) -> None:
        ops = FakeOps()
        outcome = RecoveredWorkerDeliveryUseCase(ops=ops).run(
            request(), execute=False
        )
        self.assertFalse(outcome.is_blocked)
        self.assertFalse(outcome.executed)
        self.assertEqual(WORK_ANCHOR, ops.preflight["journal"])
        self.assertEqual(
            LIFECYCLE_DECISION, ops.preflight["lifecycle_decision_journal"]
        )
        self.assertNotEqual(
            ops.preflight["journal"], ops.preflight["lifecycle_decision_journal"]
        )
        self.assertIsNone(ops.delivered)

    def test_execute_uses_new_action_and_unchanged_work_anchor(self) -> None:
        ops = FakeOps()
        outcome = RecoveredWorkerDeliveryUseCase(ops=ops).run(
            request(), execute=True
        )
        self.assertFalse(outcome.is_blocked)
        self.assertEqual(REDISPATCH_DELIVERED, outcome.redispatch)
        self.assertRegex(outcome.action_id, r"^recovery-delivery-[0-9a-f]{64}$")
        self.assertEqual(WORK_ANCHOR, ops.delivered["journal"])
        self.assertEqual(LIFECYCLE_DECISION, ops.delivered["lifecycle_decision_journal"])
        self.assertEqual(retry_of(), ops.delivered["retry_of_action_id"])

    def test_authority_refusal_is_zero_execute(self) -> None:
        ops = FakeOps(ready=False)
        outcome = RecoveredWorkerDeliveryUseCase(ops=ops).run(
            request(), execute=True
        )
        self.assertTrue(outcome.is_blocked)
        self.assertFalse(outcome.executed)
        self.assertEqual("lifecycle_decision_moved", outcome.detail)
        self.assertIsNone(ops.delivered)

    def test_missing_evidence_is_incomplete(self) -> None:
        ops = FakeOps()
        outcome = RecoveredWorkerDeliveryUseCase(ops=ops).run(
            request(prior_zero_send_journal=""), execute=True
        )
        self.assertTrue(outcome.is_blocked)
        self.assertFalse(outcome.executed)
        self.assertIsNone(ops.preflight)
        self.assertIsNone(ops.delivered)


class WorkerForwardAttemptIdentityTest(unittest.TestCase):
    def test_identity_binds_both_authority_axes_and_target_generation(self) -> None:
        base = {
            "issue": ISSUE,
            "lane": LANE,
            "workspace_id": WORKSPACE,
            "lane_generation": 1,
            "lifecycle_decision_journal": LIFECYCLE_DECISION,
            "anchor_journal": WORK_ANCHOR,
            "target_action_id": TARGET_ACTION,
            "target_assigned_name": WORKER_NAME,
        }
        first = recovered_worker_forward_attempt_id(**base)
        self.assertRegex(first, r"^worker-forward-zero-send-[0-9a-f]{64}$")
        self.assertNotEqual(
            first,
            recovered_worker_forward_attempt_id(
                **{**base, "lifecycle_decision_journal": "88146"}
            ),
        )
        self.assertNotEqual(
            first,
            recovered_worker_forward_attempt_id(
                **{**base, "anchor_journal": "88144"}
            ),
        )
        self.assertNotEqual(
            first,
            recovered_worker_forward_attempt_id(
                **{**base, "target_assigned_name": WORKER_NAME + "-foreign"}
            ),
        )


class RecoveredWorkerDeliveryLiveAuthorityTest(unittest.TestCase):
    """The live adapter rejoins both authority axes at action time."""

    def _facts(self):
        approval = "88290"
        evidence_journal = "88291"
        gateway_name = encode_assigned_name(WORKSPACE, "codex", LANE)
        worker_name = encode_assigned_name(WORKSPACE, "claude", LANE)
        retry_id = recovered_worker_forward_attempt_id(
            issue=ISSUE,
            lane=LANE,
            workspace_id=WORKSPACE,
            lane_generation=1,
            lifecycle_decision_journal=LIFECYCLE_DECISION,
            anchor_journal=WORK_ANCHOR,
            target_action_id=TARGET_ACTION,
            target_assigned_name=worker_name,
        )
        entries = (
            RedmineJournalEntry(
                issue_id=ISSUE,
                journal_id=approval,
                notes=build_recovery_delivery_authorization_marker(
                    issue=ISSUE,
                    lane=LANE,
                    workspace_id=WORKSPACE,
                    anchor_journal=WORK_ANCHOR,
                    retry_of_action_id=retry_id,
                    prior_zero_send_journal=evidence_journal,
                ),
            ),
            RedmineJournalEntry(
                issue_id=ISSUE,
                journal_id=evidence_journal,
                notes=build_recovery_delivery_zero_send_marker(
                    issue=ISSUE,
                    lane=LANE,
                    workspace_id=WORKSPACE,
                    anchor_journal=WORK_ANCHOR,
                    retry_of_action_id=retry_id,
                    target_assigned_name=worker_name,
                ),
            ),
        )
        record = SimpleNamespace(
            lane_disposition=DISPOSITION_ACTIVE,
            issue_id=ISSUE,
            decision_issue_id=ISSUE,
            decision_journal=LIFECYCLE_DECISION,
            lane_generation=1,
        )
        pair = SimpleNamespace(
            ok=True,
            gateway=SimpleNamespace(
                provider="codex",
                assigned_name=gateway_name,
            ),
            worker=SimpleNamespace(
                provider="claude",
                assigned_name=worker_name,
            ),
        )
        return SimpleNamespace(
            approval=approval,
            evidence_journal=evidence_journal,
            gateway_name=gateway_name,
            worker_name=worker_name,
            retry_id=retry_id,
            entries=entries,
            record=record,
            pair=pair,
        )

    def _ops(self, tmp: str, approval: str):
        return worker_delivery_live.LiveRecoveredWorkerDeliveryOps(
            repo_root=Path(tmp) / "wt",
            request_issue=ISSUE,
            request_lane=LANE,
            request_journal=approval,
            env={},
            lifecycle_home=Path(tmp),
            attestation_home=Path(tmp),
        )

    def _call(self, ops, facts, **changes):
        values = {
            "retry_of_action_id": facts.retry_id,
            "target_action_id": TARGET_ACTION,
            "issue": ISSUE,
            "lane": LANE,
            "journal": WORK_ANCHOR,
            "lifecycle_decision_journal": LIFECYCLE_DECISION,
            "approval_journal": facts.approval,
            "prior_zero_send_journal": facts.evidence_journal,
            "workspace_id": WORKSPACE,
        }
        values.update(changes)
        return ops.preflight_retry_redispatch_to_worker(**values)

    def test_preflight_accepts_distinct_exact_authority_axes(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts = self._facts()
            ops = self._ops(tmp, facts.approval)
            with patch.object(
                worker_delivery_live,
                "LaneLifecycleStore",
                return_value=SimpleNamespace(get=lambda key: facts.record),
            ), patch.object(
                worker_delivery_live,
                "read_declared_pin_pair",
                return_value=facts.pair,
            ), patch.object(
                type(ops),
                "_journal_entries",
                return_value=facts.entries,
            ), patch.object(
                type(ops),
                "_target_delivery_ready",
                return_value=True,
            ) as target_preflight:
                ready, detail = self._call(ops, facts)
            self.assertTrue(ready)
            self.assertEqual("ready", detail)
            self.assertEqual(2, target_preflight.call_count)

    def test_lifecycle_pointer_or_retry_identity_drift_is_zero_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts = self._facts()
            ops = self._ops(tmp, facts.approval)
            cases = (
                (
                    {"lifecycle_decision_journal": "88146"},
                    "lifecycle_decision_moved",
                ),
                (
                    {"retry_of_action_id": facts.retry_id + "-forged"},
                    "worker_retry_identity_mismatch",
                ),
            )
            for changes, expected in cases:
                with self.subTest(expected=expected), patch.object(
                    worker_delivery_live,
                    "LaneLifecycleStore",
                    return_value=SimpleNamespace(get=lambda key: facts.record),
                ), patch.object(
                    worker_delivery_live,
                    "read_declared_pin_pair",
                    return_value=facts.pair,
                ), patch.object(
                    type(ops),
                    "_journal_entries",
                    return_value=facts.entries,
                ), patch.object(
                    type(ops),
                    "_target_delivery_ready",
                    return_value=True,
                ):
                    ready, detail = self._call(ops, facts, **changes)
                self.assertFalse(ready)
                self.assertEqual(expected, detail)

    def test_execute_rejoins_full_context_after_fence_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts = self._facts()
            ops = self._ops(tmp, facts.approval)

            def redispatch(**kwargs):
                self.assertEqual(
                    facts.worker_name,
                    kwargs["gateway_assigned_name"],
                )
                self.assertTrue(kwargs["pre_send_authority"]())
                return REDISPATCH_DELIVERED

            with patch.object(
                worker_delivery_live,
                "LaneLifecycleStore",
                return_value=SimpleNamespace(get=lambda key: facts.record),
            ), patch.object(
                worker_delivery_live,
                "read_declared_pin_pair",
                return_value=facts.pair,
            ), patch.object(
                type(ops),
                "_journal_entries",
                return_value=facts.entries,
            ), patch.object(
                type(ops),
                "_target_delivery_ready",
                return_value=True,
            ), patch.object(
                type(ops),
                "_redispatch_with_action",
                side_effect=redispatch,
            ) as drive:
                result = ops.retry_redispatch_to_worker(
                    action_id="recovery-delivery-new",
                    retry_of_action_id=facts.retry_id,
                    target_action_id=TARGET_ACTION,
                    issue=ISSUE,
                    lane=LANE,
                    journal=WORK_ANCHOR,
                    lifecycle_decision_journal=LIFECYCLE_DECISION,
                    approval_journal=facts.approval,
                    prior_zero_send_journal=facts.evidence_journal,
                    workspace_id=WORKSPACE,
                )
            self.assertEqual(REDISPATCH_DELIVERED, result)
            drive.assert_called_once()


if __name__ == "__main__":
    unittest.main()
