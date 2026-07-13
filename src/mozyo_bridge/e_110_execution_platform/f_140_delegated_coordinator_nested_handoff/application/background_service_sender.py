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
from typing import Callable, Mapping, Optional, Protocol, Sequence

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
    AUTH_NO_CLAIM,
    AUTH_NO_LEASE,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (
    PANE_KEY_ID,
    PANE_KEY_LANE,
    PANE_KEY_ROLE,
    PANE_KEY_WORKSPACE,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TargetResolver(Protocol):
    """Re-resolves a claimed row's route to live targets (route ledger + live inventory)."""

    def resolve(self, row: CallbackOutboxRow) -> TargetResolution: ...


@dataclass
class BackendNeutralTargetResolver:
    """Resolve a row's target from the **backend-neutral live inventory** by stable fields (R4-F1).

    The production :class:`TargetResolver`. Per the route-identity-ledger authority model, a live
    target is the stable ``(workspace_id, lane_id, role)`` slot re-matched against the **live**
    inventory (for Herdr, the canonical assigned name; the transient locator / cached
    ``last_seen_pane_id`` is never the authority, only send-time evidence). For a claimed row it:

    - takes the row's durable expected target tuple (``target_receiver`` = the binding-resolved
      provider role recorded at enqueue, ``target_lane``) — a blank expected receiver is
      unresolvable and yields no target (fail-closed);
    - filters the live inventory to this workspace and that **exact role** (so a ``coordinator``
      route never resolves a different-role pane — e.g. a default-lane Claude — R4-F1) and, when the
      row records a lane, that lane;
    - emits a :class:`DeliveryTarget` per surviving live slot, carrying the ROW's own anchor
      (issue / journal) + expected generation and the live ``id`` as the send-time ``locator``.

    ``live_inventory`` is the injectable backend-neutral inventory seam: production adapts the live
    Herdr / tmux inventory into the neutral row shape (``neutral_inventory``); tests inject fixed
    neutral rows. An inventory read that raises degrades to an empty resolution (fail-closed).
    """

    workspace_id: str
    live_inventory: Callable[[], "Sequence[Mapping[str, object]]"]

    def resolve(self, row: CallbackOutboxRow) -> TargetResolution:
        expected_receiver = str(getattr(row, "target_receiver", "") or "").strip()
        expected_lane = str(getattr(row, "target_lane", "") or "").strip()
        if not expected_receiver:
            return TargetResolution.of([])  # no binding-resolved expected role -> fail-closed
        try:
            rows = list(self.live_inventory())
        except Exception:  # noqa: BLE001 - an unreadable inventory is a fail-closed no-target
            return TargetResolution.of([])
        targets: list[DeliveryTarget] = []
        seen: set = set()
        for entry in rows:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get(PANE_KEY_WORKSPACE, "") or "") != self.workspace_id:
                continue
            role = str(entry.get(PANE_KEY_ROLE, "") or "")
            if role != expected_receiver:
                continue  # exact binding-resolved role match (R4-F1): never a wrong-role pane
            lane = str(entry.get(PANE_KEY_LANE, "") or "")
            if expected_lane and lane != expected_lane:
                continue
            locator = str(entry.get(PANE_KEY_ID, "") or "").strip()  # send-time evidence only
            if not locator:
                continue
            key = (lane, role, locator)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                DeliveryTarget(
                    workspace_id=self.workspace_id, lane=lane, receiver=role,
                    issue=str(getattr(row, "issue", "") or ""),
                    journal=str(getattr(row, "journal", "") or ""),
                    generation=str(getattr(row, "target_generation", "") or ""),
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
    live. ``target_resolver`` re-resolves the exact target; ``transport`` performs the one send. The
    row's durable expected tuple (``target_lane`` / ``target_receiver`` / ``target_generation``) is
    what the authority binds the re-resolved live target to (R4-F2).
    """

    workspace_id: str
    holder: str
    lease_store: SupervisorLeaseStore
    target_resolver: TargetResolver
    transport: DeliveryTransport
    outbox: Optional[CallbackOutbox] = None
    now_fn: Callable[[], str] = _utc_now_iso
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
        decision = authorize_background_delivery(
            expected_workspace=self.workspace_id,
            row_workspace=str(getattr(row, "workspace_id", "") or ""),
            row_issue=str(getattr(row, "issue", "") or ""),
            row_journal=str(getattr(row, "journal", "") or ""),
            row_lane=str(getattr(row, "target_lane", "") or ""),
            row_receiver=str(getattr(row, "target_receiver", "") or ""),
            row_generation=str(getattr(row, "target_generation", "") or ""),
            has_lease=has_lease,
            has_claim=has_claim,
            resolution=resolution,
        )
        if not decision.authorized or decision.target is None:
            # Deterministic zero-send: the transport is NEVER invoked. NOT_SENT keeps the row
            # retryable (a transient authority loss is re-delivered by the real owner; a persistent
            # one bounded-retries then dead-letters) — never an uncertain that blind-retries.
            return CallbackSendResult(
                SEND_NOT_SENT, persist_ok=False, persist_reason=decision.reason
            )
        # R4-F3: the target resolution above can take time (a live-inventory read); a takeover /
        # claim recovery DURING it must still zero-send. Re-verify the lease + claim ownership
        # IMMEDIATELY before the transport injection — the delivery-authority fence closes right up
        # to the send edge, so an authority lost mid-resolution never fires the transport.
        if not self._holds_lease():
            return CallbackSendResult(SEND_NOT_SENT, persist_ok=False, persist_reason=AUTH_NO_LEASE)
        if not self._holds_claim(row):
            return CallbackSendResult(SEND_NOT_SENT, persist_ok=False, persist_reason=AUTH_NO_CLAIM)
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
    "BackendNeutralTargetResolver",
    "DeliveryTransport",
    "BackgroundServiceCallbackSender",
)
