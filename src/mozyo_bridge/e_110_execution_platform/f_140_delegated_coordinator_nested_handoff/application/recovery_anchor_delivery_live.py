"""Operator-capable, target-pinned recovery-anchor delivery service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Mapping, Optional

from mozyo_bridge.core.state.herdr_delivery_ledger import record_herdr_delivery
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    BINDING_BOUND,
    HerdrIdentityReplacementBindingStore,
    replacement_action_is_bound,
    selected_attestation_store_is_v1,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    build_marker,
    build_notification_body,
    normalize_anchor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    DETAIL_ATTESTATION_MISMATCH,
    DETAIL_ATTESTATION_UNREADABLE,
    DETAIL_OK,
    DETAIL_PRECONDITION_NOT_IDLE,
    DETAIL_RAIL_UNAVAILABLE,
    DETAIL_TARGET_IDENTITY_MISMATCH,
    DETAIL_TARGET_NOT_LIVE,
    DETAIL_TARGET_NOT_SETTLED,
    DETAIL_TARGET_RETIRING,
    DETAIL_TARGET_REVISION_MISMATCH,
    DETAIL_TARGET_UNRESOLVED,
    DETAIL_TURN_START_UNCONFIRMED,
    DETAIL_WORKSPACE_MISMATCH,
    DETAIL_AUTHORITY_MOVED,
    DISPOSITION_STARTED,
    DISPOSITION_UNCERTAIN,
    DISPOSITION_ZERO_SEND,
    RecoveryAnchorDeliveryPreflight,
    RecoveryAnchorDeliveryOutcome,
    RecoveryAnchorDeliveryRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_TURN_ENDED,
    map_agent_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_LIVE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (  # noqa: E501
    make_resend_screen_guard,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (  # noqa: E501
    OUTCOME_PRECONDITION_NOT_IDLE,
    OUTCOME_STARTED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    HerdrCliTransport,
    Runner,
    resolve_herdr_binary,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

_STATUS_KEYS = ("agent_status", "status", "state")


def _norm(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _row_runtime_state(row: Mapping[str, object]) -> str:
    for key in _STATUS_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return map_agent_status(value)
    return map_agent_status(None)


@dataclass(frozen=True)
class _DeliveryPreflight:
    marker: str
    rail: object | None
    blocker: RecoveryAnchorDeliveryOutcome | None

    @property
    def ready(self) -> bool:
        return self.rail is not None and self.blocker is None


class LiveRecoveryAnchorDeliveryService:
    """Deliver one closed-kind durable anchor to one exact fresh receiver.

    The public constructor intentionally carries only live runtime dependencies.
    Tests override the narrow protected read/build methods; production always
    uses the real inventory, attestation, retirement guard, rail, and ledger.
    """

    def __init__(
        self,
        repo_root: Path,
        env: Mapping[str, str],
        runner: Optional[Runner] = None,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        attestation_home: Optional[Path] = None,
        pre_transport_authority: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.env = env
        self.runner = runner
        self.timeout = timeout
        self.attestation_home = attestation_home
        #: Optional action-time authority re-joined AFTER the delivery preflight and
        #: IMMEDIATELY before ``drive_turn_start`` (Redmine #14475, review j#88538 F1).
        #: ``None`` keeps every pre-#14475 caller byte-invariant.
        self.pre_transport_authority = pre_transport_authority

    def ready(self) -> bool:
        """Whether the operator-capable high-level turn-start rail resolves."""
        return self._build_rail() is not None

    def preflight(
        self, request: RecoveryAnchorDeliveryRequest
    ) -> RecoveryAnchorDeliveryPreflight:
        """Run every read-only action gate without injecting or writing a ledger."""

        resolved = self._preflight(request)
        if resolved.blocker is not None:
            return RecoveryAnchorDeliveryPreflight(
                may_deliver=False,
                detail=resolved.blocker.detail,
                marker=resolved.marker,
            )
        return RecoveryAnchorDeliveryPreflight(
            may_deliver=True,
            detail=DETAIL_OK,
            marker=resolved.marker,
        )

    def deliver(
        self, request: RecoveryAnchorDeliveryRequest
    ) -> RecoveryAnchorDeliveryOutcome:
        # Re-run the complete read-only preflight at the irreversible edge.  A
        # prior public preflight is advisory and never reused as authority.
        resolved = self._preflight(request)
        if resolved.blocker is not None:
            return resolved.blocker
        rail = resolved.rail
        marker = resolved.marker
        assert rail is not None  # established by _DeliveryPreflight.ready
        anchor = normalize_anchor(
            "redmine", issue=_norm(request.issue), journal=_norm(request.journal)
        )
        body = build_notification_body(anchor, request.kind, None, request.provider)
        # Redmine #14475 (review j#88538 F1): the LAST external observation before transport is
        # ``_preflight`` above (target resolution + rail readiness), so an authority checked by
        # the caller BEFORE ``deliver`` is not an authority checked before the send. This seam
        # is the final re-join; a drift here is a typed zero-send with no injection attempted.
        if self.pre_transport_authority is not None:
            try:
                still_authorized = bool(self.pre_transport_authority())
            except (Exception, SystemExit):  # noqa: BLE001 - unreadable authority is not current
                still_authorized = False
            if not still_authorized:
                outcome = RecoveryAnchorDeliveryOutcome(
                    disposition=DISPOSITION_ZERO_SEND,
                    detail=DETAIL_AUTHORITY_MOVED,
                )
                self._record(outcome, request, turn_start_telemetry=None)
                return outcome
        try:
            # Redmine #15202: bind the receiver provider's declared startup screens into
            # the rail's WAIT_ERROR Enter-resend gate (unbound, that resend is withheld).
            result = rail.drive_turn_start(
                request.target_locator,
                f"{marker} {body}",
                screen_guard=make_resend_screen_guard(request.provider),
            )
        except (Exception, SystemExit):  # injection may have happened
            outcome = self._uncertain(marker)
            self._record(outcome, request, turn_start_telemetry=None)
            return outcome

        turn_start_outcome = _norm(getattr(result, "outcome", ""))
        telemetry = self._telemetry(result)
        if turn_start_outcome == OUTCOME_STARTED:
            outcome = RecoveryAnchorDeliveryOutcome(
                disposition=DISPOSITION_STARTED,
                detail=DETAIL_OK,
                marker=marker,
                turn_start_outcome=turn_start_outcome,
            )
        elif turn_start_outcome == OUTCOME_PRECONDITION_NOT_IDLE:
            outcome = RecoveryAnchorDeliveryOutcome(
                disposition=DISPOSITION_ZERO_SEND,
                detail=DETAIL_PRECONDITION_NOT_IDLE,
                marker=marker,
                turn_start_outcome=turn_start_outcome,
            )
        else:
            outcome = self._uncertain(marker, turn_start_outcome)
        self._record(outcome, request, turn_start_telemetry=telemetry)
        return outcome

    def _preflight(self, request: RecoveryAnchorDeliveryRequest) -> _DeliveryPreflight:
        anchor = normalize_anchor(
            "redmine", issue=_norm(request.issue), journal=_norm(request.journal)
        )
        marker = build_marker(anchor, request.kind, request.provider)

        rail = self._build_rail()
        if rail is None:
            return _DeliveryPreflight(
                marker, None, self._zero(DETAIL_RAIL_UNAVAILABLE, marker)
            )

        try:
            workspace_id = _norm(self._workspace_id())
        except Exception:  # noqa: BLE001 - unreadable authority is a zero-send
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_WORKSPACE_MISMATCH, marker)
            )
        if workspace_id != _norm(request.workspace_id):
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_WORKSPACE_MISMATCH, marker)
            )

        try:
            rows = self._rows()
        except Exception:  # noqa: BLE001 - unreadable live inventory is a zero-send
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_UNRESOLVED, marker)
            )
        named = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(request.target_assigned_name)
        ]
        if len(named) != 1:
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_UNRESOLVED, marker)
            )
        row = named[0]
        if _norm(_agent_locator(row)) != _norm(request.target_locator):
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_IDENTITY_MISMATCH, marker)
            )

        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decoded.ok or decoded.identity is None:
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_IDENTITY_MISMATCH, marker)
            )
        identity = decoded.identity
        if not (
            _norm(identity.workspace_id) == _norm(request.workspace_id)
            and _norm(identity.lane_id) == _norm(request.lane_id)
            and _norm(identity.role) == _norm(request.provider)
        ):
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_IDENTITY_MISMATCH, marker)
            )
        detected_provider = _norm(row.get("agent"))
        if detected_provider and detected_provider != _norm(request.provider):
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_IDENTITY_MISMATCH, marker)
            )
        if classify_named_slot(row) != SLOT_LIVE:
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_NOT_LIVE, marker)
            )
        if _row_runtime_state(row) not in (
            RUNTIME_TURN_ENDED,
            RUNTIME_AWAITING_INPUT,
        ):
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_NOT_SETTLED, marker)
            )
        if _norm(row.get("revision")) != _norm(request.target_revision):
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_REVISION_MISMATCH, marker)
            )

        try:
            attestation = self._read_attestation(request.target_assigned_name)
        except Exception:  # noqa: BLE001 - unreadable attestation is a zero-send
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_ATTESTATION_UNREADABLE, marker)
            )
        if attestation is None:
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_ATTESTATION_MISMATCH, marker)
            )
        if not (
            _norm(getattr(attestation, "assigned_name", ""))
            == _norm(request.target_assigned_name)
            and _norm(getattr(attestation, "workspace_id", ""))
            == _norm(request.workspace_id)
            and _norm(getattr(attestation, "lane_id", ""))
            == _norm(request.lane_id)
            and _norm(getattr(attestation, "role", "")) == _norm(request.provider)
            and _norm(getattr(attestation, "locator", ""))
            == _norm(request.target_locator)
            and _norm(getattr(attestation, "verdict", "")) == VERDICT_PRESENT
            and bool(_norm(getattr(attestation, "observed_at", "")))
        ):
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_ATTESTATION_MISMATCH, marker)
            )
        if not self._attestation_bound_to_action(attestation, request):
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_ATTESTATION_MISMATCH, marker)
            )

        try:
            retiring, _reason = self._target_is_retiring(
                request.target_assigned_name
            )
        except Exception:  # noqa: BLE001 - unreadable retirement authority fails closed
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_RETIRING, marker)
            )
        if retiring:
            return _DeliveryPreflight(
                marker, rail, self._zero(DETAIL_TARGET_RETIRING, marker)
            )
        return _DeliveryPreflight(marker, rail, None)

    def _workspace_id(self) -> str:
        return repo_scope_workspace_id(self.repo_root)

    def _rows(self):
        return list_herdr_agent_rows(self.env)

    def _read_attestation(self, assigned_name: str):
        return HerdrIdentityAttestationStore(home=self.attestation_home).read(
            _norm(assigned_name)
        )

    def _target_is_retiring(self, assigned_name: str) -> tuple[bool, str]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (  # noqa: E501
            target_is_retiring,
        )

        return target_is_retiring(assigned_name)

    def _attestation_bound_to_action(
        self, attestation: object, request: RecoveryAnchorDeliveryRequest
    ) -> bool:
        """Join either native-v2 action binding or the v1 side-binding authority."""
        home = self.attestation_home or mozyo_bridge_home()
        try:
            is_v1 = selected_attestation_store_is_v1(home)
        except Exception:  # noqa: BLE001 - unknown store generation fails closed
            return False
        if not is_v1:
            return (
                _norm(getattr(attestation, "replacement_action_id", ""))
                == _norm(request.target_action_id)
            )
        try:
            binding = HerdrIdentityReplacementBindingStore(home=home).read(
                _norm(request.target_action_id),
                _norm(request.target_assigned_name),
            )
        except Exception:  # noqa: BLE001 - unreadable side authority fails closed
            return False
        return bool(
            binding is not None
            and binding.phase == BINDING_BOUND
            and replacement_action_is_bound(
                attestation,
                action_id=_norm(request.target_action_id),
                live_locator=_norm(request.target_locator),
                expected_workspace_id=_norm(request.workspace_id),
                expected_role=_norm(request.provider),
                expected_lane=_norm(request.lane_id),
                expected_assigned_name=_norm(request.target_assigned_name),
                expected_old_locator=_norm(binding.old_locator),
                home=home,
            )
        )

    def _build_rail(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (  # noqa: E501
            HerdrTurnStartRail,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (  # noqa: E501
            HerdrCliAgentStateReader,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_turn_start import (  # noqa: E501
            HerdrCliWaitPrimitive,
        )

        try:
            resolution = resolve_herdr_binary(self.env)
            transport = HerdrCliTransport(
                resolution.path, runner=self.runner, timeout=self.timeout
            )
            reader = HerdrCliAgentStateReader(
                resolution.path, runner=self.runner, timeout=self.timeout
            )
            wait = HerdrCliWaitPrimitive(resolution.path)
            return HerdrTurnStartRail(transport=transport, reader=reader, wait=wait)
        except Exception:  # noqa: BLE001 - unavailable capability is a zero-send
            return None

    def _record(
        self,
        outcome: RecoveryAnchorDeliveryOutcome,
        request: RecoveryAnchorDeliveryRequest,
        *,
        turn_start_telemetry: Optional[dict],
    ) -> None:
        if outcome.started:
            status, reason = "sent", "ok"
        elif outcome.zero_send:
            status, reason = "blocked", outcome.detail
        else:
            status, reason = "uncertain", outcome.detail
        record_herdr_delivery(
            SimpleNamespace(
                status=status,
                reason=reason,
                notification_marker=outcome.marker,
                source="redmine",
                anchor={
                    "source": "redmine",
                    "issue": _norm(request.issue),
                    "journal": _norm(request.journal),
                },
                receiver=_norm(request.provider),
                target=_norm(request.target_locator),
                mode="oneshot",
                turn_start_outcome=turn_start_telemetry,
            ),
            provider=_norm(request.provider),
            backend="herdr",
            home=self.attestation_home,
        )

    @staticmethod
    def _telemetry(result: object) -> Optional[dict]:
        projection = getattr(result, "to_telemetry_dict", None)
        if not callable(projection):
            return None
        try:
            value = projection()
        except Exception:  # noqa: BLE001 - ledger telemetry is best effort
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _zero(detail: str, marker: str) -> RecoveryAnchorDeliveryOutcome:
        return RecoveryAnchorDeliveryOutcome(
            disposition=DISPOSITION_ZERO_SEND,
            detail=detail,
            marker=marker,
        )

    @staticmethod
    def _uncertain(
        marker: str, turn_start_outcome: str = ""
    ) -> RecoveryAnchorDeliveryOutcome:
        return RecoveryAnchorDeliveryOutcome(
            disposition=DISPOSITION_UNCERTAIN,
            detail=DETAIL_TURN_START_UNCONFIRMED,
            marker=marker,
            turn_start_outcome=turn_start_outcome,
        )


__all__ = ["LiveRecoveryAnchorDeliveryService"]
