from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_delivery_ledger import HerdrDeliveryLedger
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    recovery_anchor_delivery_live as delivery_live,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_anchor_delivery_live import (  # noqa: E501
    LiveRecoveryAnchorDeliveryService,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    DETAIL_ATTESTATION_MISMATCH,
    DETAIL_PRECONDITION_NOT_IDLE,
    DETAIL_TARGET_IDENTITY_MISMATCH,
    DETAIL_TARGET_NOT_LIVE,
    DETAIL_TARGET_NOT_SETTLED,
    DETAIL_TARGET_RETIRING,
    DETAIL_TARGET_REVISION_MISMATCH,
    DETAIL_TARGET_UNRESOLVED,
    DETAIL_TURN_START_UNCONFIRMED,
    DETAIL_WORKSPACE_MISMATCH,
    DISPOSITION_STARTED,
    DISPOSITION_UNCERTAIN,
    DISPOSITION_ZERO_SEND,
    RecoveryAnchorDeliveryRequest,
    recovery_delivery_action_id,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (  # noqa: E501
    OUTCOME_ABSENT,
    OUTCOME_BLOCKED,
    OUTCOME_DELIVERED_NOT_STARTED,
    OUTCOME_INJECT_FAILED,
    OUTCOME_PRECONDITION_NOT_IDLE,
    OUTCOME_STARTED,
    TurnStartResult,
)

WORKSPACE = "giken-3800-mozyo-bridge"
LANE = "issue_14203_pair_recovery_r17"
PROVIDER = "claude"
LOCATOR = "s:17"
REVISION = "31"
ACTION_ID = "recover-action-r17"
ASSIGNED_NAME = encode_assigned_name(WORKSPACE, PROVIDER, LANE)


def request(**changes: str) -> RecoveryAnchorDeliveryRequest:
    values = {
        "issue": "14203",
        "journal": "88159",
        "kind": "reply",
        "workspace_id": WORKSPACE,
        "lane_id": LANE,
        "provider": PROVIDER,
        "target_assigned_name": ASSIGNED_NAME,
        "target_locator": LOCATOR,
        "target_revision": REVISION,
        "target_action_id": ACTION_ID,
    }
    values.update(changes)
    return RecoveryAnchorDeliveryRequest(**values)


def live_row(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "name": ASSIGNED_NAME,
        "pane_id": LOCATOR,
        "agent": PROVIDER,
        "status": "done",
        "revision": REVISION,
    }
    values.update(changes)
    return values


def attestation(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "assigned_name": ASSIGNED_NAME,
        "workspace_id": WORKSPACE,
        "role": PROVIDER,
        "lane_id": LANE,
        "locator": LOCATOR,
        "verdict": "present",
        "observed_at": "2026-07-26T00:00:00+00:00",
        "replacement_action_id": ACTION_ID,
    }
    values.update(changes)
    return SimpleNamespace(**values)


class FakeRail:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def drive_turn_start(self, target: str, body: str):
        self.calls.append((target, body))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeLiveService(LiveRecoveryAnchorDeliveryService):
    def __init__(
        self,
        home: Path,
        *,
        rail: FakeRail | None,
        workspace_id: str = WORKSPACE,
        rows: object = None,
        attested: object = None,
        retiring: bool = False,
    ) -> None:
        super().__init__(
            repo_root=home,
            env={},
            attestation_home=home,
        )
        self.rail = rail
        self.workspace_id = workspace_id
        self.rows = [live_row()] if rows is None else rows
        self.attested = attestation() if attested is None else attested
        self.retiring = retiring

    def _build_rail(self):
        return self.rail

    def _workspace_id(self) -> str:
        return self.workspace_id

    def _rows(self):
        if isinstance(self.rows, BaseException):
            raise self.rows
        return self.rows

    def _read_attestation(self, assigned_name: str):
        if isinstance(self.attested, BaseException):
            raise self.attested
        return self.attested

    def _target_is_retiring(self, assigned_name: str) -> tuple[bool, str]:
        return (self.retiring, "retirement_guard" if self.retiring else "")


class RecoveryAnchorDeliveryDomainTest(unittest.TestCase):
    def test_request_exposes_no_body_or_role_and_kind_is_closed(self) -> None:
        fields = {field.name for field in dataclasses.fields(RecoveryAnchorDeliveryRequest)}
        self.assertNotIn("body", fields)
        self.assertNotIn("role", fields)
        with self.assertRaises(ValueError):
            request(kind="custom")
        with self.assertRaises(ValueError):
            request(target_revision="")

    def test_action_id_is_stable_complete_and_authority_sensitive(self) -> None:
        values = {
            "issue": "14203",
            "lane": LANE,
            "approval_journal": "88159",
            "anchor_journal": "88143",
            "retry_of_action_id": "pair-retry-r16",
            "prior_zero_send_journal": "88155",
        }
        first = recovery_delivery_action_id(**values)
        reversed_values = dict(reversed(tuple(values.items())))
        self.assertEqual(first, recovery_delivery_action_id(**reversed_values))
        self.assertRegex(first, r"^recovery-delivery-[0-9a-f]{64}$")
        self.assertNotEqual(
            first,
            recovery_delivery_action_id(**{**values, "approval_journal": "88160"}),
        )
        with self.assertRaises(ValueError):
            recovery_delivery_action_id(**{**values, "prior_zero_send_journal": ""})


class RecoveryAnchorDeliveryLiveTest(unittest.TestCase):
    def _home(self):
        return tempfile.TemporaryDirectory()

    def test_started_is_the_only_delivered_result_and_writes_real_ledger(self) -> None:
        with self._home() as raw_home:
            home = Path(raw_home)
            rail = FakeRail(TurnStartResult(outcome=OUTCOME_STARTED))
            result = FakeLiveService(home, rail=rail).deliver(request())

            self.assertEqual(DISPOSITION_STARTED, result.disposition)
            self.assertTrue(result.started)
            self.assertEqual(1, len(rail.calls))
            target, body = rail.calls[0]
            self.assertEqual(LOCATOR, target)
            self.assertIn("source=redmine:issue=14203:journal=88159:kind=reply:to=claude", body)
            records = HerdrDeliveryLedger(home=home).records_for_marker(result.marker)
            self.assertEqual(1, len(records))
            record = records[0]
            self.assertEqual("sent", record.status)
            self.assertEqual("ok", record.reason)
            self.assertEqual("event_rail", record.rail)
            self.assertEqual(OUTCOME_STARTED, record.turn_start_outcome["outcome"])
            self.assertEqual(LOCATOR, record.target)

    def test_every_action_time_target_pin_fails_closed_before_drive(self) -> None:
        other_lane_name = encode_assigned_name(WORKSPACE, PROVIDER, "other_lane")
        cases = (
            (
                DETAIL_WORKSPACE_MISMATCH,
                {"workspace_id": "other-workspace"},
                {},
            ),
            (
                DETAIL_TARGET_UNRESOLVED,
                {"rows": []},
                {},
            ),
            (
                DETAIL_TARGET_UNRESOLVED,
                {"rows": [live_row(), live_row()]},
                {},
            ),
            (
                DETAIL_TARGET_IDENTITY_MISMATCH,
                {"rows": [live_row(pane_id="s:18")]},
                {},
            ),
            (
                DETAIL_TARGET_IDENTITY_MISMATCH,
                {
                    "rows": [live_row(name=other_lane_name)],
                    "attested": attestation(assigned_name=other_lane_name),
                },
                {"target_assigned_name": other_lane_name},
            ),
            (
                DETAIL_TARGET_NOT_LIVE,
                {"rows": [live_row(agent="")]},
                {},
            ),
            (
                DETAIL_TARGET_IDENTITY_MISMATCH,
                {"rows": [live_row(agent="codex")]},
                {},
            ),
            (
                DETAIL_TARGET_NOT_SETTLED,
                {"rows": [live_row(status="working")]},
                {},
            ),
            (
                DETAIL_TARGET_REVISION_MISMATCH,
                {"rows": [live_row(revision="32")]},
                {},
            ),
            (
                DETAIL_ATTESTATION_MISMATCH,
                {"attested": attestation(replacement_action_id="foreign-action")},
                {},
            ),
        )
        for expected, service_changes, request_changes in cases:
            with self.subTest(expected=expected), self._home() as raw_home:
                rail = FakeRail(TurnStartResult(outcome=OUTCOME_STARTED))
                result = FakeLiveService(
                    Path(raw_home), rail=rail, **service_changes
                ).deliver(request(**request_changes))
                self.assertEqual(DISPOSITION_ZERO_SEND, result.disposition)
                self.assertEqual(expected, result.detail)
                self.assertEqual([], rail.calls)

    def test_attestation_rejoins_every_identity_axis_and_action_binding(self) -> None:
        changes = (
            {"assigned_name": "foreign"},
            {"workspace_id": "foreign"},
            {"lane_id": "foreign"},
            {"role": "codex"},
            {"locator": "s:99"},
            {"verdict": "missing"},
            {"observed_at": ""},
            {"replacement_action_id": "foreign"},
        )
        for changed in changes:
            with self.subTest(changed=changed), self._home() as raw_home:
                rail = FakeRail(TurnStartResult(outcome=OUTCOME_STARTED))
                result = FakeLiveService(
                    Path(raw_home), rail=rail, attested=attestation(**changed)
                ).deliver(request())
                self.assertEqual(DISPOSITION_ZERO_SEND, result.disposition)
                self.assertEqual(DETAIL_ATTESTATION_MISMATCH, result.detail)
                self.assertEqual([], rail.calls)

    def test_v1_attestation_requires_the_exact_bound_side_authority(self) -> None:
        with self._home() as raw_home:
            service = FakeLiveService(
                Path(raw_home),
                rail=FakeRail(TurnStartResult(outcome=OUTCOME_STARTED)),
            )
            binding = SimpleNamespace(phase="bound", old_locator="s:OLD")
            store = SimpleNamespace(read=lambda action, assigned_name: binding)
            with patch.object(
                delivery_live, "selected_attestation_store_is_v1", return_value=True
            ), patch.object(
                delivery_live,
                "HerdrIdentityReplacementBindingStore",
                return_value=store,
            ), patch.object(
                delivery_live, "replacement_action_is_bound", return_value=True
            ) as joined:
                self.assertTrue(
                    service._attestation_bound_to_action(attestation(), request())
                )
            joined.assert_called_once()
            with patch.object(
                delivery_live, "selected_attestation_store_is_v1", return_value=True
            ), patch.object(
                delivery_live,
                "HerdrIdentityReplacementBindingStore",
                return_value=store,
            ), patch.object(
                delivery_live, "replacement_action_is_bound", return_value=False
            ):
                self.assertFalse(
                    service._attestation_bound_to_action(attestation(), request())
                )

    def test_retirement_guard_is_the_final_pre_drive_zero_send(self) -> None:
        with self._home() as raw_home:
            rail = FakeRail(TurnStartResult(outcome=OUTCOME_STARTED))
            result = FakeLiveService(
                Path(raw_home), rail=rail, retiring=True
            ).deliver(request())
            self.assertEqual(DISPOSITION_ZERO_SEND, result.disposition)
            self.assertEqual(DETAIL_TARGET_RETIRING, result.detail)
            self.assertEqual([], rail.calls)

    def test_preinjection_rail_refusal_is_typed_zero_send_and_recorded(self) -> None:
        with self._home() as raw_home:
            home = Path(raw_home)
            rail = FakeRail(
                TurnStartResult(outcome=OUTCOME_PRECONDITION_NOT_IDLE)
            )
            result = FakeLiveService(home, rail=rail).deliver(request())
            self.assertEqual(DISPOSITION_ZERO_SEND, result.disposition)
            self.assertEqual(DETAIL_PRECONDITION_NOT_IDLE, result.detail)
            records = HerdrDeliveryLedger(home=home).records_for_marker(result.marker)
            self.assertEqual("blocked", records[0].status)
            self.assertEqual(DETAIL_PRECONDITION_NOT_IDLE, records[0].reason)

    def test_all_postinjection_or_unknown_rail_results_are_uncertain(self) -> None:
        outcomes = (
            OUTCOME_DELIVERED_NOT_STARTED,
            OUTCOME_BLOCKED,
            OUTCOME_ABSENT,
            OUTCOME_INJECT_FAILED,
        )
        for rail_outcome in outcomes:
            with self.subTest(rail_outcome=rail_outcome), self._home() as raw_home:
                home = Path(raw_home)
                result = FakeLiveService(
                    home,
                    rail=FakeRail(TurnStartResult(outcome=rail_outcome)),
                ).deliver(request())
                self.assertEqual(DISPOSITION_UNCERTAIN, result.disposition)
                self.assertEqual(DETAIL_TURN_START_UNCONFIRMED, result.detail)
                records = HerdrDeliveryLedger(home=home).records_for_marker(result.marker)
                self.assertEqual("uncertain", records[0].status)
                self.assertEqual(rail_outcome, records[0].turn_start_outcome["outcome"])

    def test_drive_exception_is_uncertain_because_injection_cannot_be_excluded(self) -> None:
        with self._home() as raw_home:
            home = Path(raw_home)
            result = FakeLiveService(
                home, rail=FakeRail(RuntimeError("drive failed"))
            ).deliver(request())
            self.assertEqual(DISPOSITION_UNCERTAIN, result.disposition)
            records = HerdrDeliveryLedger(home=home).records_for_marker(result.marker)
            self.assertEqual("uncertain", records[0].status)

    def test_unavailable_rail_is_zero_send(self) -> None:
        with self._home() as raw_home:
            service = FakeLiveService(Path(raw_home), rail=None)
            self.assertFalse(service.ready())
            result = service.deliver(request())
            self.assertEqual(DISPOSITION_ZERO_SEND, result.disposition)

    def test_ready_reports_the_same_rail_capability_used_by_deliver(self) -> None:
        with self._home() as raw_home:
            rail = FakeRail(TurnStartResult(outcome=OUTCOME_STARTED))
            self.assertTrue(FakeLiveService(Path(raw_home), rail=rail).ready())


if __name__ == "__main__":
    unittest.main()
