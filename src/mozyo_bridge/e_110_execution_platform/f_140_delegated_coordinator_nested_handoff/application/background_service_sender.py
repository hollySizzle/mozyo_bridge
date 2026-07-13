"""Background-service callback sender (Redmine #13683 R2-F3 design answer j#77216, Model A').

The supervisor's callback ``send_fn``, delivering a claimed outbox row as a dedicated
``background_service`` authority — NOT an agent :class:`SenderIdentity`. It ties the pure
authorization (:mod:`...domain.background_service_delivery`) to three injected seams:

- the **lease store** — the send-time proof that this authority still holds the workspace supervisor
  lease (boundary 2: lease + claim). A lease read that is missing / not-ours / expired -> zero-send.
- the **target resolver** (a seam) — re-resolves the row's route against the route ledger + live
  inventory immediately before send (boundary 4). 0 / >1 / foreign-workspace -> fail-closed.
- the **transport** (a seam) — performs the one delivery, sharing the handoff rail's outcome
  vocabulary but under a **separated origin class** (boundary 5); the live wire is the Phase B
  dogfood seam, and the mechanism here is exercised through the seam in the isolated E2E.

Outcome mapping (the closed :data:`SEND_OUTCOMES` the processor consumes):

- a fail-closed authorization (no lease / no claim / foreign / no or ambiguous target / generation
  mismatch) is a **deterministic not-sent** (:data:`SEND_NOT_SENT`) — the transport is NEVER
  invoked, and the row stays retryable so a transient loss (the lease moved) is re-delivered by the
  real owner and a persistent one (no target ever resolves) bounded-retries then dead-letters;
- an authorized delivery maps the transport's ``(status, reason)`` through the shared conservative
  :func:`...domain.callback_delivery.send_outcome_for_delivery` — a confirmed turn-start is
  delivered, a genuine ambiguous/uncertain outcome is :data:`SEND_UNCERTAIN` with **no blind retry**
  (boundary 5), preserving the dead-letter / reconciliation contract.

This origin never originates a request: it only ever delivers a :class:`CallbackOutboxRow` the
existing classifier persisted (boundary 3 — enforced by construction, the processor hands it a
claimed row).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from mozyo_bridge.core.state.callback_outbox import (
    CALLBACK_CLAIM_LEASE_SECONDS,
    CallbackOutbox,
    CallbackOutboxRow,
)
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.background_service_delivery import (
    AUTH_OK,
    DeliveryTarget,
    TargetResolution,
    authorize_background_delivery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    CallbackSendResult,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
    send_outcome_for_delivery,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TargetResolver(Protocol):
    """Re-resolves a claimed row's route to live targets (route ledger + live inventory)."""

    def resolve(self, row: CallbackOutboxRow) -> TargetResolution: ...


@dataclass
class RouteLedgerTargetResolver:
    """Resolve a row's callback route to live targets from the route ledger + live inventory (R3-F2).

    The production :class:`TargetResolver`: for a claimed row it reads the durable **route ledger**
    (:meth:`WorkflowRuntimeStore.read_route_identities`), keeps only this workspace's identities in
    the coordinator lane the route names, and **cross-checks each against the live inventory** — a
    ledger entry whose ``last_seen_pane_id`` is not in the live locator set is dropped, so a stale
    ledger row never delivers to a dead pane. Each surviving identity becomes a
    :class:`DeliveryTarget` carrying the ROW's own anchor (issue / journal), so the sender's
    anchor-binding authority (R3-F3) passes for a legitimately-resolved target and fails closed for a
    drifted one. 0 / >1 live targets are surfaced as-is (the authority fail-closes on either).

    ``live_locators`` is the injectable live-inventory seam: production wires the workspace-scoped
    agent discovery; the isolated 2-workspace test injects a fixed live set. A store / inventory read
    that raises degrades to an empty resolution (fail-closed).
    """

    workspace_id: str
    store: object
    live_locators: Callable[[], "set[str]"]
    coordinator_lanes: tuple = ("default", "coordinator")

    def resolve(self, row: CallbackOutboxRow) -> TargetResolution:
        route = str(getattr(row, "callback_route", "") or "").strip()
        try:
            identities = self.store.read_route_identities()
        except Exception:  # noqa: BLE001 - an unreadable ledger is a fail-closed no-target
            return TargetResolution.of([])
        try:
            live = set(self.live_locators())
        except Exception:  # noqa: BLE001 - an unreadable inventory drops every live cross-check
            live = set()
        targets: list[DeliveryTarget] = []
        seen: set = set()
        for identity in identities:
            if str(getattr(identity, "workspace_id", "") or "") != self.workspace_id:
                continue
            lane = str(getattr(identity, "lane_id", "") or "")
            if route == "coordinator" and lane not in self.coordinator_lanes:
                continue
            locator = str(getattr(identity, "last_seen_pane_id", "") or "").strip()
            if not locator or locator not in live:
                continue  # live-inventory cross-check: a dead / unknown pane is never a target
            role = str(getattr(identity, "role", "") or "")
            key = (lane, role, locator)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                DeliveryTarget(
                    workspace_id=self.workspace_id, lane=lane, receiver=role,
                    issue=str(getattr(row, "issue", "") or ""),
                    journal=str(getattr(row, "journal", "") or ""),
                    locator=locator,
                )
            )
        return TargetResolution.of(targets)


class DeliveryTransport(Protocol):
    """Performs one background-service delivery to a resolved target; returns its outcome."""

    def deliver(self, row: CallbackOutboxRow, target: DeliveryTarget) -> HandoffDeliveryResult: ...


@dataclass
class BackgroundServiceCallbackSender:
    """A callback ``send_fn`` that delivers under the ``background_service`` authority (fail-closed).

    ``workspace_id`` / ``holder`` identify the authority (the lease held by this supervisor process
    for this workspace). ``lease_store`` is read at send time to confirm the lease is still ours and
    live. ``target_resolver`` re-resolves the exact target; ``transport`` performs the one send.
    ``expected_generation_fn`` (optional) maps a row to a generation constraint (a no-op returning
    ``""`` in Phase A; the forward hook for #13684's correlated generation).
    """

    workspace_id: str
    holder: str
    lease_store: SupervisorLeaseStore
    target_resolver: TargetResolver
    transport: DeliveryTransport
    outbox: Optional[CallbackOutbox] = None
    now_fn: Callable[[], str] = _utc_now_iso
    expected_generation_fn: Optional[Callable[[CallbackOutboxRow], str]] = None
    claim_stale_seconds: int = CALLBACK_CLAIM_LEASE_SECONDS

    def __call__(self, row: CallbackOutboxRow) -> CallbackSendResult:
        # Boundary 2 (claim): the durable outbox claim must still be OURS and unexpired at send time
        # (R3-F4) — a non-empty in-memory token is not proof. When an outbox is wired, re-verify the
        # claim against the store; a de-owned / lease-expired / absent claim fails closed. Without an
        # outbox (a pure-mechanism test) fall back to the token presence.
        has_claim = self._holds_claim(row)
        # Boundary 2 (lease): a still-live workspace supervisor lease held by THIS authority.
        has_lease = self._holds_lease()
        # Boundary 4: re-resolve the exact target now (an unresolvable route -> fail-closed no target).
        try:
            resolution = self.target_resolver.resolve(row)
        except Exception:  # noqa: BLE001 - an unreadable route/inventory is a fail-closed no-target
            resolution = TargetResolution.of([])
        expected_generation = (
            self.expected_generation_fn(row) if self.expected_generation_fn is not None else ""
        )
        decision = authorize_background_delivery(
            expected_workspace=self.workspace_id,
            row_workspace=str(getattr(row, "workspace_id", "") or ""),
            row_issue=str(getattr(row, "issue", "") or ""),
            row_journal=str(getattr(row, "journal", "") or ""),
            has_lease=has_lease,
            has_claim=has_claim,
            resolution=resolution,
            expected_generation=expected_generation or "",
        )
        if not decision.authorized or decision.target is None:
            # Deterministic zero-send: the transport is NEVER invoked. NOT_SENT keeps the row
            # retryable (a transient authority loss is re-delivered by the real owner; a persistent
            # one bounded-retries then dead-letters) — never an uncertain that blind-retries.
            return CallbackSendResult(
                SEND_NOT_SENT, persist_ok=False, persist_reason=decision.reason
            )
        try:
            result = self.transport.deliver(row, decision.target)
        except Exception:  # noqa: BLE001 - a transport blow-up mid-send is uncertain (no blind retry)
            return CallbackSendResult(SEND_UNCERTAIN, persist_reason="transport_error")
        outcome = send_outcome_for_delivery(result.status, result.reason)
        return CallbackSendResult(
            outcome, persist_ok=result.persist_ok, persist_reason=result.persist_reason
        )

    def _holds_claim(self, row: CallbackOutboxRow) -> bool:
        """True iff this row's outbox claim is still OURS and unexpired at send time (R3-F4).

        When an ``outbox`` is wired (production), re-verify the row's ``claim_token`` against the
        durable store — a de-owned (recovered + re-claimed elsewhere) or lease-expired claim fails
        closed. Without an outbox (a pure-mechanism test that already controls the token) fall back
        to the token presence, so the authority logic stays testable in isolation.
        """
        token = str(getattr(row, "claim_token", "") or "").strip()
        if not token:
            return False
        if self.outbox is None:
            return True
        try:
            return self.outbox.verify_claim(
                row.key, token, now=self.now_fn(), stale_seconds=self.claim_stale_seconds
            )
        except Exception:  # noqa: BLE001 - an unreadable outbox is a fail-closed no-claim
            return False

    def _holds_lease(self) -> bool:
        """True iff a live workspace lease held by this ``holder`` exists at send time (fail-closed)."""
        try:
            lease = self.lease_store.holder_of(self.workspace_id)
        except Exception:  # noqa: BLE001 - an unreadable lease store is a fail-closed no-lease
            return False
        if lease is None or lease.holder != self.holder:
            return False
        # ISO-8601 UTC-second timestamps sort chronologically, so a live lease has expires_at > now.
        return str(lease.expires_at) > self.now_fn()


__all__ = (
    "TargetResolver",
    "RouteLedgerTargetResolver",
    "DeliveryTransport",
    "BackgroundServiceCallbackSender",
)
