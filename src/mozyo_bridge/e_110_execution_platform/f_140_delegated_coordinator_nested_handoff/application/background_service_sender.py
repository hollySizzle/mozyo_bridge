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
    REVIEW_ROUND_CURRENT,
    REVIEW_ROUND_STALE,
    REVIEW_ROUND_UNVERIFIABLE,
    REVIEW_ROUND_DISPOSITIONS,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
    send_outcome_for_delivery,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: The action-time round-fence refusal (Redmine #13684 R1-F1 / #13974 R8-F1): a reserved review_result
#: return was found DETERMINISTICALLY stale at the send edge (a *readable* provider superseded its
#: round — a newer review_request / result / correction, an identity drift, or an ambiguous / conflicting
#: identity). A terminal zero-send — the transport is never invoked and the row is marked terminally
#: (retry 0, operator-visible), never a bounded-retry pending row that keeps #13974's backlog alive.
ROUND_STALE = "review_round_stale"
#: The action-time round-fence could not re-verify the round because the provider read failed
#: transiently (source unresolvable / ``None`` / markers unreadable). A retryable zero-send — the
#: transport is never invoked but the row bounded-retries, so a genuinely-current callback that hit a
#: transient outage is re-delivered rather than terminally dropped (#13974 R8-F1).
ROUND_UNVERIFIABLE = "review_round_unverifiable"


class TargetResolver(Protocol):
    """Re-resolves a claimed row's route to live targets (route ledger + live inventory)."""

    def resolve(self, row: CallbackOutboxRow) -> TargetResolution: ...


#: The backend-neutral live inventory the resolver re-matches against, as ``(rows, backend)`` — the
#: raw backend inventory (Herdr ``agent list`` / tmux) + the backend token so the delegation
#: normalizes and matches it through the one authority. ``Callable[[], (rows, backend)]``.
BackendInventory = Callable[[], "tuple[Sequence[Mapping[str, object]], str]"]


@dataclass
class BackendNeutralTargetResolver:
    """Resolve a row's target by delegating to the ledger's backend-neutral route authority (R5-F1).

    Per the route-identity-ledger Authority Model, a live target is the stable
    ``(workspace_id, lane_id, role, pane_name)`` identity re-matched against the LIVE inventory
    through :func:`...domain.backend_neutral_resolver.resolve_route_neutral` — which owns the
    fail-closed outcome table (ambiguity fail-closed, stale-cache detection, blank-locator downgrade,
    ``last_seen`` as evidence only). This resolver does NOT re-implement that match: it builds the
    **expected** stable identity from the row's durable tuple (``target_receiver`` role +
    ``target_lane``, with the canonical ``pane_name`` = ``encode_assigned_name``) and delegates.

    - a blank expected receiver is unresolvable -> no target (fail-closed);
    - the Herdr backend delegates to the authority (which matches the full stable key incl. the
      assigned-name ``pane_name``, so a wrong / missing label is never resolved — R5-F1);
    - an **unsupported backend** (no Phase A live-inventory adaptation, e.g. tmux) is an explicit
      durable fail-closed boundary — no target, rather than silently resolving on a partial key;
    - the resolved target's ``generation`` is read from an independent **live** authority
      (``live_generation_fn``), NEVER copied from the row (R5-F2 / #13684 correction 1): the delivery
      authority then requires the row's *expected* generation and this *live* generation to both be
      non-blank and match. Without a ``live_generation_fn`` (Phase A / a coordinator route) the live
      generation is blank, so a generation-correlated (non-blank expected) row still fails closed —
      #13684 injects the owning-lane generation reader only for the correlated review_result return
      route (:data:`...domain.review_return_route.REVIEW_RETURN_ROUTE_PREFIX`), which is what enables
      that route's delivery while leaving every other route's Phase A fail-closed-disabled state intact.

    ``inventory`` is the injectable ``(rows, backend)`` seam; tests inject fixed rows + a backend.
    ``live_generation_fn`` is the injectable independent live-generation authority (default: none ->
    blank -> unchanged Phase A behaviour).
    """

    workspace_id: str
    inventory: BackendInventory
    live_generation_fn: Optional[Callable[[CallbackOutboxRow], str]] = None

    def resolve(self, row: CallbackOutboxRow) -> TargetResolution:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (
            BACKEND_HERDR,
            herdr_route_identity,
            resolve_route_neutral,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (
            RESOLVE_OK,
        )

        expected_receiver = str(getattr(row, "target_receiver", "") or "").strip()
        expected_lane = str(getattr(row, "target_lane", "") or "").strip()
        if not expected_receiver:
            return TargetResolution.of([])  # no binding-resolved expected role -> fail-closed
        try:
            rows, backend = self.inventory()
        except Exception:  # noqa: BLE001 - an unreadable inventory is a fail-closed no-target
            return TargetResolution.of([])
        if str(backend or "").strip() != BACKEND_HERDR:
            # Unsupported backend live-inventory adaptation is an explicit Phase A fail-closed
            # boundary (the tmux live path is a Phase B dogfood surface) — never a partial-key match.
            return TargetResolution.of([])
        identity = herdr_route_identity(
            workspace_id=self.workspace_id,
            role=expected_receiver,
            route_id=f"{self.workspace_id}:{expected_lane or 'default'}:{expected_receiver}",
            lane_id=expected_lane,
        )
        try:
            resolution = resolve_route_neutral(identity, list(rows), backend=backend)
        except Exception:  # noqa: BLE001 - an authority error is a fail-closed no-target
            return TargetResolution.of([])
        if resolution.status != RESOLVE_OK:
            return TargetResolution.of([])
        locator = str(getattr(resolution, "resolved_pane_id", "") or "").strip()
        if not locator:
            return TargetResolution.of([])
        # R5-F2 / #13684 correction 1: the generation is read from the INDEPENDENT live authority
        # (``live_generation_fn``), never copied from the row. For the correlated review_result return
        # route this is the owning-lane's live revision (which mismatches a supersession-bumped or
        # owner-changed expectation -> zero-send); without an injected authority it stays blank, so a
        # generation-correlated row still fails closed at :func:`authorize_background_delivery`.
        live_generation = ""
        if self.live_generation_fn is not None:
            try:
                live_generation = str(self.live_generation_fn(row) or "").strip()
            except Exception:  # noqa: BLE001 - an unreadable generation authority is a blank -> fail-closed
                live_generation = ""
        return TargetResolution.of([
            DeliveryTarget(
                workspace_id=self.workspace_id,
                lane=expected_lane,
                receiver=expected_receiver,
                issue=str(getattr(row, "issue", "") or ""),
                journal=str(getattr(row, "journal", "") or ""),
                generation=live_generation,
                locator=locator,
            )
        ])


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
    #: #13684 R1-F1 / #13974 R8-F1 — the action-time round fence. For a correlated review_result return
    #: row it re-reads the issue's live structured markers at the send edge and returns a THREE-state
    #: disposition (:data:`REVIEW_ROUND_CURRENT` / ``STALE`` / ``UNVERIFIABLE``): current -> deliver, a
    #: deterministically-superseded round -> terminal zero-send (retry 0), a transiently-unreadable
    #: provider -> retryable zero-send. A legacy ``bool`` fence is still accepted (True=current,
    #: False=deterministic stale). ``None`` (default) applies no fence — a non-return row / a
    #: pure-mechanism test is unaffected.
    round_fence_fn: Optional[Callable[[CallbackOutboxRow], object]] = None

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
        # R1-F1 / R8-F1 action-time round fence: for a correlated review_result return, re-verify at the
        # send edge that the reserved result is STILL the current review round. The transport is NEVER
        # invoked on any refusal, but the DISPOSITION of the zero-send differs (R8-F1):
        #  - a DETERMINISTIC supersession (a readable provider says the round is stale / invalid) is
        #    TERMINAL (SEND_UNCERTAIN -> mark_uncertain: retry 0, operator-visible) so #13974's
        #    backlog-retention failure does not survive as a bounded-retry pending row;
        #  - a TRANSIENT unreadable provider is RETRYABLE (SEND_NOT_SENT -> bounded retry) so a
        #    genuinely-current callback that hit an outage is re-delivered, never terminally dropped.
        disposition = self._round_disposition(row)
        if disposition == REVIEW_ROUND_STALE:
            return CallbackSendResult(
                SEND_UNCERTAIN, persist_ok=False, persist_reason=ROUND_STALE
            )
        if disposition == REVIEW_ROUND_UNVERIFIABLE:
            return CallbackSendResult(
                SEND_NOT_SENT, persist_ok=False, persist_reason=ROUND_UNVERIFIABLE
            )
        try:
            result = self.transport.deliver(row, decision.target)
        except Exception:  # noqa: BLE001 - a transport blow-up mid-send is uncertain (no blind retry)
            return CallbackSendResult(SEND_UNCERTAIN, persist_reason="transport_error")
        outcome = send_outcome_for_delivery(result.status, result.reason)
        return CallbackSendResult(
            outcome, persist_ok=result.persist_ok, persist_reason=result.persist_reason
        )

    def _round_disposition(self, row: CallbackOutboxRow) -> str:
        """Classify the row's reserved review round at the send edge (R1-F1 / R8-F1).

        Returns a member of :data:`REVIEW_ROUND_DISPOSITIONS`:
        - no fence wired -> :data:`REVIEW_ROUND_CURRENT` (a non-return row / pure-mechanism test is
          unaffected);
        - a modern fence returns the disposition token directly;
        - a legacy ``bool`` fence maps True -> ``CURRENT`` and False -> ``STALE`` (a bool "not the
          current round" is a deterministic supersession, hence terminal);
        - a fence that raises, or returns an unrecognized value, is :data:`REVIEW_ROUND_UNVERIFIABLE`
          — we could not re-verify the round, so it is retryable, never terminally dropped (a possibly
          genuinely-current callback must not be lost to a transient fence failure).
        """
        if self.round_fence_fn is None:
            return REVIEW_ROUND_CURRENT
        try:
            result = self.round_fence_fn(row)
        except Exception:  # noqa: BLE001 - an unverifiable round is retryable (not terminally dropped)
            return REVIEW_ROUND_UNVERIFIABLE
        if isinstance(result, bool):
            return REVIEW_ROUND_CURRENT if result else REVIEW_ROUND_STALE
        if result in REVIEW_ROUND_DISPOSITIONS:
            return str(result)
        return REVIEW_ROUND_UNVERIFIABLE

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
    "ROUND_STALE",
    "ROUND_UNVERIFIABLE",
    "TargetResolver",
    "BackendNeutralTargetResolver",
    "DeliveryTransport",
    "BackgroundServiceCallbackSender",
)
