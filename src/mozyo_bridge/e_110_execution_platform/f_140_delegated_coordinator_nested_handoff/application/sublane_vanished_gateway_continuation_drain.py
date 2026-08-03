"""Ledger-confirmed exactly-once continuation for a recovered vanished gateway (#14741).

The B6b2 recovery is driven first, then the stored continuation is joined to one fresh,
action-bound gateway.  A transaction reaches ``completed`` only through the unchanged
shared :func:`drive_continuation_once` state machine and an exact post-attestation record in
the canonical herdr delivery ledger.  An attempted but unconfirmed send is never replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Protocol

from mozyo_bridge.core.state.herdr_delivery_ledger import (
    BACKEND_HERDR,
    RAIL_QUEUE_ENTER,
    HerdrDeliveryLedger,
    HerdrDeliveryLedgerRecord,
)
from mozyo_bridge.core.state.replacement_transaction import (
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    CAS_GENERATION_MISMATCH,
    CAS_LEASE_CONFLICT,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    ContinuationPointer,
    ParticipantPin,
    PHASE_COMPLETED,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    RedmineAnchor,
    build_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_ERROR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
    DEFAULT_LEASE_TTL_SECONDS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_continuation_drain import (  # noqa: E501
    CONTINUATION_AUTHORITY_MOVED,
    CONTINUATION_CONFIRMED,
    CONTINUATION_GENERATION_MISMATCH,
    CONTINUATION_LEASE_LOST,
    CONTINUATION_NOT_FOUND,
    CONTINUATION_UNREADABLE,
    drive_continuation_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_continuation import (  # noqa: E501
    CONTINUATION_READY,
    ContinuationPreparation,
    prepare_vanished_gateway_continuation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_continuation_send import (  # noqa: E501
    VanishedGatewayContinuationOps,
    VanishedGatewaySendAuthority,
    VanishedGatewaySendResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery_live import (  # noqa: E501
    recovery_lease_holder,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

ACTION_GENERATION = 1
_CLAIM_RETRY_CAP = 8


def _plain(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        return ""
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _strictly_after(left: object, right: object) -> bool:
    """Compare aware ISO timestamps; malformed or timezone-less values fail closed."""

    left_text = _plain(left)
    right_text = _plain(right)
    if not left_text or not right_text:
        return False
    try:
        left_time = datetime.fromisoformat(left_text.replace("Z", "+00:00"))
        right_time = datetime.fromisoformat(right_text.replace("Z", "+00:00"))
        if left_time.utcoffset() is None or right_time.utcoffset() is None:
            return False
        return left_time > right_time
    except (TypeError, ValueError, OverflowError):
        return False


def _lease_expiry(now: object, ttl: object) -> Optional[str]:
    if not _plain(now) or type(ttl) is not int or ttl <= 0:
        return None
    try:
        base = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if base.utcoffset() is None:
            return None
        return (base + timedelta(seconds=ttl)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def _authority_is_for_preparation(
    preparation: object, authority: object
) -> bool:
    if (
        type(preparation) is not ContinuationPreparation
        or type(authority) is not VanishedGatewaySendAuthority
        or type(preparation.pointer) is not ContinuationPointer
        or type(preparation.participant) is not ParticipantPin
    ):
        return False
    pin = preparation.participant
    axes = (
        authority.action_id,
        authority.workspace_id,
        authority.lane_id,
        authority.provider,
        authority.assigned_name,
        authority.fresh_locator,
        authority.old_locator,
        authority.observed_at,
    )
    return (
        all(_plain(value) for value in axes)
        and type(authority.revision) is int
        and authority.revision >= 0
        and authority.action_id == preparation.action_id
        and authority.workspace_id == pin.evidence_workspace_id
        and authority.lane_id == pin.lane_id
        and authority.provider == pin.provider
        and authority.assigned_name == pin.assigned_name
        and authority.old_locator == pin.old_locator
        and authority.fresh_locator != authority.old_locator
    )


class _PreAttemptConfirmationStop(RuntimeError):
    """Typed internal short-circuit before the shared driver's attempted CAS."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class VanishedGatewayContinuationDrainResult:
    """Closed continuation disposition; it carries no transport or host detail."""

    status: str
    action_id: str = ""

    @property
    def completed(self) -> bool:
        return self.status == CONTINUATION_CONFIRMED


class VanishedGatewayContinuationPort(Protocol):
    """The read/recheck/send surface the exactly-once use case consumes."""

    def context_is_exact(self) -> bool: ...

    def current_authority(
        self, preparation: object
    ) -> Optional[VanishedGatewaySendAuthority]: ...

    def send_once(self, preparation: object) -> VanishedGatewaySendResult: ...


@dataclass
class VanishedGatewayContinuationDrain:
    """Compose the canonical send adapter, ledger predicate, lease, and shared CAS drive."""

    store: ReplacementTransactionStore
    ops: VanishedGatewayContinuationPort
    clock: Callable[[], str] = _utc_now
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS

    def _records_for_marker(self, marker: str) -> object:
        """The only ledger read surface used by this continuation."""

        return HerdrDeliveryLedger(home=mozyo_bridge_home()).records_for_marker(marker)

    def _confirmation(
        self,
        preparation: ContinuationPreparation,
        authority: VanishedGatewaySendAuthority,
    ) -> Optional[bool]:
        """True/False for a readable exact ledger; None means unreadable authority."""

        pointer = preparation.pointer
        if (
            not _authority_is_for_preparation(preparation, authority)
            or type(pointer) is not ContinuationPointer
            or _plain(getattr(pointer, "source", None)) != "redmine"
        ):
            return None
        try:
            marker = build_marker(
                RedmineAnchor(
                    issue=pointer.issue_id,
                    journal=pointer.journal_id,
                ),
                "implementation_request",
                authority.provider,
            )
            records = self._records_for_marker(marker)
        except (Exception, SystemExit):
            return None
        if type(records) is not list:
            return None
        for record in records:
            if type(record) is not HerdrDeliveryLedgerRecord:
                continue
            try:
                provider = record.provider
                provider_ok = (
                    provider is None
                    or type(provider) is str
                    and provider in ("", authority.provider)
                )
                if (
                    record.notification_marker == marker
                    and record.source == "redmine"
                    and record.issue_id == pointer.issue_id
                    and record.journal_id == pointer.journal_id
                    and record.receiver == authority.provider
                    and provider_ok
                    and record.backend == BACKEND_HERDR
                    and record.rail == RAIL_QUEUE_ENTER
                    and record.target == authority.fresh_locator
                    and record.target != authority.old_locator
                    and record.status == "sent"
                    and record.reason == "ok"
                    and _strictly_after(record.recorded_at, authority.observed_at)
                ):
                    return True
            except (Exception, SystemExit):
                return None
        return False

    def _ensure_lease(
        self,
        key: ReplacementTransactionKey,
        *,
        holder: str,
    ) -> Optional[str]:
        """Keep a live same-action lease, or reclaim a free/expired one with its holder."""

        for _ in range(_CLAIM_RETRY_CAP):
            try:
                record = self.store.get(key)
                if record is None:
                    return CONTINUATION_NOT_FOUND
                if (
                    type(record.action_generation) is not int
                    or record.action_generation != ACTION_GENERATION
                ):
                    return CONTINUATION_GENERATION_MISMATCH
                if record.phase == PHASE_COMPLETED:
                    return None
                now = self.clock()
                if record.lease_holder == holder and record.lease_is_live(now):
                    return None
                expires = _lease_expiry(now, self.lease_ttl_seconds)
                if expires is None:
                    return CONTINUATION_UNREADABLE
                outcome = self.store.claim(
                    key,
                    expected_revision=record.revision,
                    expected_action_generation=ACTION_GENERATION,
                    holder=holder,
                    lease_expires_at=expires,
                    now=now,
                )
            except (Exception, SystemExit):
                return CONTINUATION_UNREADABLE
            try:
                if outcome.applied:
                    return None
                reason = outcome.reason
            except (Exception, SystemExit):
                return CONTINUATION_UNREADABLE
            if reason == CAS_STALE_REVISION:
                continue
            if reason == CAS_GENERATION_MISMATCH:
                return CONTINUATION_GENERATION_MISMATCH
            if reason == CAS_NOT_FOUND:
                return CONTINUATION_NOT_FOUND
            if reason == CAS_LEASE_CONFLICT:
                return CONTINUATION_LEASE_LOST
            return CONTINUATION_UNREADABLE
        return CONTINUATION_UNREADABLE

    def drive(self, preparation: object) -> VanishedGatewayContinuationDrainResult:
        """Drive one prepared continuation, completing only on exact ledger evidence."""

        if (
            type(preparation) is not ContinuationPreparation
            or preparation.outcome != CONTINUATION_READY
            or not _plain(preparation.action_id)
            or not _plain(preparation.holder)
            or preparation.holder != recovery_lease_holder(preparation.action_id)
        ):
            return VanishedGatewayContinuationDrainResult(CONTINUATION_UNREADABLE)
        try:
            context_exact = self.ops.context_is_exact()
            authority = self.ops.current_authority(preparation)
        except (Exception, SystemExit):
            return VanishedGatewayContinuationDrainResult(
                CONTINUATION_UNREADABLE, preparation.action_id
            )
        if not context_exact:
            return VanishedGatewayContinuationDrainResult(
                CONTINUATION_UNREADABLE, preparation.action_id
            )
        if not _authority_is_for_preparation(preparation, authority):
            return VanishedGatewayContinuationDrainResult(
                CONTINUATION_AUTHORITY_MOVED, preparation.action_id
            )
        try:
            key = ReplacementTransactionKey(authority.workspace_id, preparation.action_id)
        except (Exception, SystemExit):
            return VanishedGatewayContinuationDrainResult(
                CONTINUATION_UNREADABLE, preparation.action_id
            )
        # Read once before the lease mutation so an unreadable ledger never becomes
        # permission to claim or send.  This value is deliberately NOT cached for the
        # driver's idempotency-first check: a matching record may land while the lease is
        # acquired/reclaimed, and the check adjacent to the attempted CAS must see it.
        if self._confirmation(preparation, authority) is None:
            return VanishedGatewayContinuationDrainResult(
                CONTINUATION_UNREADABLE, preparation.action_id
            )
        lease_failure = self._ensure_lease(key, holder=preparation.holder)
        if lease_failure is not None:
            return VanishedGatewayContinuationDrainResult(
                lease_failure, preparation.action_id
            )

        first_confirmation = True

        def confirmed() -> bool:
            nonlocal first_confirmation
            # Every answer is fresh.  In particular, the driver's first call follows the
            # lease acquisition and is adjacent to its attempted CAS, closing the window in
            # which an already-landed continuation could otherwise be sent a second time.
            is_first = first_confirmation
            first_confirmation = False
            current = self.ops.current_authority(preparation)
            if not _authority_is_for_preparation(preparation, current):
                if is_first:
                    # The idempotency barrier cannot inspect the exact ledger without its
                    # exact target authority.  A transient move is not proof of ledger
                    # absence and must not open an attempted-CAS/send path if the authority
                    # happens to rejoin on the driver's next read.
                    raise _PreAttemptConfirmationStop(
                        CONTINUATION_AUTHORITY_MOVED
                    )
                return False
            confirmation = self._confirmation(preparation, current)
            if confirmation is None and is_first:
                # The driver's first confirmation is the idempotency barrier immediately
                # before its attempted CAS.  Unreadable is not evidence of absence there:
                # fail closed before the transaction can become sendable.  Later (post-send)
                # unreadable observations retain the driver's uncertain semantics.
                raise _PreAttemptConfirmationStop(CONTINUATION_UNREADABLE)
            return confirmation is True

        def authority_current() -> bool:
            return _authority_is_for_preparation(
                preparation, self.ops.current_authority(preparation)
            )

        def send() -> str:
            try:
                result = self.ops.send_once(preparation)
            except (Exception, SystemExit):
                return DRAIN_SEND_ERROR
            if type(result) is not VanishedGatewaySendResult:
                return DRAIN_SEND_ERROR
            return result.status

        try:
            status = drive_continuation_once(
                self.store,
                self.clock,
                key,
                holder=preparation.holder,
                gen=ACTION_GENERATION,
                authority_fn=authority_current,
                send_fn=send,
                confirmed_fn=confirmed,
            )
        except _PreAttemptConfirmationStop as stopped:
            status = stopped.status
        except (Exception, SystemExit):
            status = CONTINUATION_UNREADABLE
        return VanishedGatewayContinuationDrainResult(status, preparation.action_id)


def drive_vanished_gateway_continuation(
    *,
    plan: Any,
    anchor: Any,
    store: Any,
    home: Any,
    workspace_id: str,
    actuation_port: Any,
    repo_root: Any,
    upstream_coordinator: str,
    launch_authority: Any = None,
    store_admission: Any = None,
    clock: Optional[Callable[[], str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> VanishedGatewayContinuationDrainResult:
    """Production composition: B6b2 recovery first, then the exactly-once continuation."""

    preparation = prepare_vanished_gateway_continuation(
        plan=plan,
        anchor=anchor,
        store=store,
        home=home,
        workspace_id=workspace_id,
        actuation_port=actuation_port,
        launch_authority=launch_authority,
        store_admission=store_admission,
        clock=clock,
    )
    if type(preparation) is not ContinuationPreparation or not preparation.ready:
        status = _plain(getattr(preparation, "stopped", None)) or CONTINUATION_UNREADABLE
        return VanishedGatewayContinuationDrainResult(
            status, _plain(getattr(preparation, "action_id", None))
        )
    ops_kwargs: dict[str, object] = {
        "repo_root": repo_root,
        "upstream_coordinator": upstream_coordinator,
    }
    if env is not None:
        ops_kwargs["env"] = env
    ops = VanishedGatewayContinuationOps(**ops_kwargs)
    return VanishedGatewayContinuationDrain(
        store=store,
        ops=ops,
        clock=clock or _utc_now,
    ).drive(preparation)


__all__ = (
    "ACTION_GENERATION",
    "VanishedGatewayContinuationDrain",
    "VanishedGatewayContinuationPort",
    "VanishedGatewayContinuationDrainResult",
    "drive_vanished_gateway_continuation",
)
