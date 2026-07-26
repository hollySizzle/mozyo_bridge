"""The recovery redelivery's reserve -> send -> record EDGE (Redmine #14475).

Extracted from the live recover-pair adapter as a leaf (module-health threshold; behaviour
unchanged). Keeping it whole and separate is also the point of review j#88571 F1 / j#88579 F1:
this is the only place that observes what the transport and the outbox writes actually did, and
it reports those observations as a :class:`RedispatchEdgeResult` instead of a status the
application would have to re-infer facts from.

``ops`` is the live adapter, passed explicitly rather than captured: the edge uses its fence,
its gateway resolution, and its checkout-authority re-join, and nothing else.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFenceError,
    FENCE_CANCELLED,
    FENCE_DELIVERED,
    FENCE_RESERVED,
    FENCE_UNCERTAIN,
    FenceKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    KIND_IMPLEMENTATION_REQUEST,
    RecoveryAnchorDeliveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
    RedispatchEdgeResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
    REDISPATCH_FAILED,
    REDISPATCH_TARGET_RETIRING,
    REDISPATCH_UNCERTAIN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
    decode_assigned_name,
)


def perform_redispatch(
    ops: Any,
    *,
    action_id: str,
    target_action_id: str,
    gateway_assigned_name: str,
    issue: str,
    lane: str,
    journal: str,
    workspace_id: str,
    pre_send_authority: Optional[Callable[[], bool]] = None,
) -> RedispatchEdgeResult:
    # Review j#88571 F1: this edge reports what it OBSERVED (sent? settled? unknown?), not
    # a status the application would have to re-infer those facts from.
    _edge = ops._edge_result
    key = FenceKey(
        workspace_id=_norm(workspace_id), lane_id=_norm_lane(lane), issue=_norm(issue),
        journal=_norm(journal), action_id=_norm(action_id),
        target_assigned_name=_norm(gateway_assigned_name),
    )
    fence = ops._fence()
    # Fail closed on a missing / lost / inconsistent fence (Redmine #13847 R1-F2): only an
    # already-bootstrapped, identity-matched fence can prove exactly-once. An un-bootstrapped
    # or lost store is a reconcile condition, never a fresh reserve that could re-send.
    try:
        bootstrapped = fence.is_bootstrapped()
    except Exception:  # noqa: BLE001 - unreadable fence state => uncertain (never send)
        return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
    if not bootstrapped:
        return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
    def _record(mark: Any, detail: str) -> Optional[bool]:
        """Observe a fence write: ``True`` wrote, ``False`` row vanished, ``None`` raised.

        Review j#88579 F1 / probe j#88576: ``mark_delivered`` / ``mark_cancelled`` return a
        BOOL (the rowcount) — a vanished or already-terminal row is a ``False``, not an
        exception. Discarding it reported a settled state that was never written. One
        observer for every write site so a later branch cannot re-drop it.
        """
        try:
            wrote = bool(mark(key, detail=detail))
        except DispatchOutboxFenceError:
            return None
        if not wrote:
            # The UPDATE matched nothing: the reserved row VANISHED between the reserve and
            # this outcome write. Leaving it gone would drop the exactly-once hold entirely,
            # so ``record_uncertain`` re-asserts the fail-closed ``uncertain`` terminal for
            # the key — a later reserve then sees ``uncertain`` and never re-sends. It only
            # moves a key TOWARD uncertain, so a real delivery is never downgraded.
            try:
                fence.record_uncertain(
                    key, detail=f"reserved row missing at outcome write: {detail}"
                )
            except DispatchOutboxFenceError:
                pass
        return wrote

    try:
        reserve = fence.reserve(key)
    except DispatchOutboxFenceError:
        return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
    if not reserve.won:
        # The fence already holds a row for this exact redispatch — idempotent. A
        # delivered/reserved-by-another row is "already"; an uncertain one needs reconcile.
        # Review j#88579 F2: classify the loser's state TOTALLY. Treating everything that
        # is not ``uncertain`` as "already delivered" promoted a CANCELLED row — a durable
        # zero-send whose implementation_request was never delivered — into a silent
        # success.
        state = _norm(reserve.current_state)
        if reserve.needs_reconcile or state in (FENCE_UNCERTAIN, FENCE_RESERVED):
            # Owed or unknown: the send's fate is not established.
            return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
        if state == FENCE_DELIVERED:
            # Positively delivered by an earlier run: this run sent nothing, state settled.
            return _edge(REDISPATCH_ALREADY, zero_send=True)
        if state == FENCE_CANCELLED:
            # Settled, but NOT delivered: a blocked zero-send, never an "already" success.
            return _edge(REDISPATCH_FAILED, zero_send=True)
        # An unrecognised state is never degraded to success.
        return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
    # We won the reserve. Before resolving a locator or sending, the shared retirement
    # guard (Redmine #13892 R6-F3): this is a reserve -> send edge like every other, and
    # `target_is_retiring`'s own docstring already named this call site. A send into panes
    # a retirement transaction is closing either lands in a doomed pane or races the
    # close, so the reserve is cancelled — never left reserved, which would read as an
    # unresolved send fate and block the retirement it just deferred to.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (  # noqa: E501
        target_is_retiring,
    )

    retiring, why = target_is_retiring(_norm(gateway_assigned_name))
    if retiring:
        wrote = _record(fence.mark_cancelled, f"target retiring: {why}")
        if wrote is None:
            return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
        # Nothing was sent either way, but only a WRITTEN cancel settles the durable state.
        if not wrote:
            return _edge(REDISPATCH_TARGET_RETIRING, unknown_fate=True)
        return _edge(REDISPATCH_TARGET_RETIRING, zero_send=True)
    if pre_send_authority is not None:
        try:
            still_authorized = bool(pre_send_authority())
        except (Exception, SystemExit):
            still_authorized = False
        if not still_authorized:
            wrote = _record(
                fence.mark_cancelled,
                "recovery delivery action-time authority moved; zero-send",
            )
            if wrote is None:
                return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
            # Only a WRITTEN cancel is a KNOWN zero-send (review j#88579 F1).
            if not wrote:
                return _edge(REDISPATCH_FAILED, unknown_fate=True)
            return _edge(REDISPATCH_FAILED, zero_send=True)
    gateway_locator, target_revision = ops._gateway_live_target(
        gateway_assigned_name
    )
    decoded = decode_assigned_name(gateway_assigned_name)
    if (
        not gateway_locator
        or not target_revision
        or not decoded.ok
        or decoded.identity is None
    ):
        wrote = _record(
            fence.mark_cancelled, "exact live gateway target unresolved; zero-send"
        )
        if wrote is None:
            return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
        if not wrote:
            return _edge(REDISPATCH_FAILED, unknown_fate=True)
        return _edge(REDISPATCH_FAILED, zero_send=True)
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_anchor_delivery_live import (  # noqa: E501
            LiveRecoveryAnchorDeliveryService,
        )

        outcome = LiveRecoveryAnchorDeliveryService(
            repo_root=ops.repo_root,
            env=ops.env,
            runner=ops.runner,
            timeout=ops.timeout,
            attestation_home=ops.attestation_home,
            # Redmine #14475 (review j#88538 F1): the re-join the service performs AFTER
            # its own target-resolution preflight and immediately before transport. The
            # ``pre_send_authority`` the reserve edge uses fires earlier, so it cannot
            # cover a drift that happens during that preflight.
            pre_transport_authority=lambda: ops._checkout_authority_current(
                ops.request_lane
            ),
        ).deliver(
            RecoveryAnchorDeliveryRequest(
                issue=_norm(issue),
                journal=_norm(journal),
                kind=KIND_IMPLEMENTATION_REQUEST,
                workspace_id=_norm(workspace_id),
                lane_id=_norm_lane(lane),
                provider=_norm(decoded.identity.role),
                target_assigned_name=_norm(gateway_assigned_name),
                target_locator=gateway_locator,
                target_revision=target_revision,
                target_action_id=_norm(target_action_id),
            )
        )
    except (Exception, SystemExit):  # noqa: BLE001 - never leave a won reserve pending
        # Review j#88587 F1: ``mark_uncertain`` returns the SAME rowcount bool as the other
        # outcome writes, so these two branches bypassed the observer and lost the hold on a
        # vanished row exactly as the delivered/cancelled sites did. Every fence write in this
        # edge goes through ``_record``; a structural regression pins that there are no others.
        _record(fence.mark_uncertain, "recovery delivery service raised")
        return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
    if outcome.started:
        wrote = _record(
            fence.mark_delivered,
            "implementation_request recovery turn-start confirmed",
        )
        if wrote is None:
            # Review j#88571 F1 / probe j#88570: the transport POSITIVELY started, so the
            # redelivery is a known-applied effect even though the ledger write failed.
            # Reporting a bare ``uncertain`` here lost that fact downstream.
            return _edge(REDISPATCH_UNCERTAIN, delivered=True, unknown_fate=True)
        # Review j#88579 F1: a ``False`` here means the row vanished — the send happened
        # but the fence never recorded it, so the state is owed, not delivered.
        if not wrote:
            return _edge(REDISPATCH_UNCERTAIN, delivered=True, unknown_fate=True)
        return _edge(REDISPATCH_DELIVERED, delivered=True)
    if outcome.zero_send:
        wrote = _record(
            fence.mark_cancelled,
            f"recovery delivery zero-send: {outcome.detail}",
        )
        if wrote is None:
            return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)
        # The service reported a zero-send; only a WRITTEN cancel settles the fence row.
        if not wrote:
            return _edge(REDISPATCH_FAILED, unknown_fate=True)
        return _edge(REDISPATCH_FAILED, zero_send=True)
    _record(fence.mark_uncertain, f"recovery delivery uncertain: {outcome.detail}")
    return _edge(REDISPATCH_UNCERTAIN, unknown_fate=True)


__all__ = ("perform_redispatch",)
