"""Herdr worker-dispatch authority and IO adapter (#13357, #13846).

The adapter joins lifecycle generation, startup attestation, live receiver state,
action binding and prior delivery; reserves the shared ``DispatchOutboxFence``;
then separates queue ACK from ledger-backed turn-start.  It never closes or
relaunches a slot, and only a durable delivered fence outcome can promote.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_worker_dispatcher as _worker_dispatcher,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SublaneLaneView,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
    SenderDispatchAdmission,
    WorkerDispatchAdmission,
    WorkerDispatchAdmissionFacts,
    WorkerDispatchRequest,
    decide_worker_dispatch_admission,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)


def _generation_binding_detail(
    *,
    current: bool,
    worker_pin,
    declared_slots_absent: bool,
    declared_reason: str,
    declared_identity_current: bool,
    declared_attestation_current: bool,
    live_generation_current: bool,
    live_present: bool,
    fresh_live_provider_consistent: bool = True,
) -> str:
    """A value-free token naming WHICH generation authority did not bind (Redmine #13846 R4).

    The prior conflict reason ("... not bound to the current declared process generation") did
    not say WHICH authority field failed, so a recurrence could not be diagnosed from the public
    structured outcome (installed acceptance finding j#82030). This returns a token identifying
    the failing sub-authority — a declared-pin identity mismatch, a live locator / runtime
    revision divergence from the declared pin, a startup self-attestation not generation-bound,
    a slot-less fresh row whose live provider disagrees (review F1) or whose live attestation is
    not generation-bound, or an unresolvable declared-pin shape (carrying only the pair-resolution
    reason token). Every token is a vocabulary label — no locator, provider VALUE, raw output, or
    secret is exposed. Empty when the binding is current.
    """
    if current:
        return ""
    if worker_pin is not None:
        if not declared_identity_current:
            return "declared_worker_identity_mismatch"
        if live_present and not live_generation_current:
            return "live_locator_or_runtime_revision_diverged_from_declared_pin"
        if not declared_attestation_current:
            return "startup_self_attestation_not_generation_bound_to_declared_pin"
        return "declared_generation_unbound"
    if declared_slots_absent:
        # Redmine #13846 R4 review F1: distinguish a wrong live provider from an unattested /
        # stale slot so the operator sees the failing identity axis (a field label, not a value).
        if not fresh_live_provider_consistent:
            return "fresh_live_provider_mismatch"
        return "fresh_startup_self_attestation_not_generation_bound"
    return f"declared_slots_unresolved:{declared_reason}"


@dataclass
class HerdrWorkerDispatchOps:
    """Live herdr adapter composing the same-lane worker-forward primitives (#13357).

    ``repo_root`` is the lane worktree the drive runs in (the gateway's own checkout —
    the same value the request's ``worktree_path`` carries). ``lane_label`` / ``issue``
    are the requested lane identity, echoed by the inventory read-back exactly like the
    #13331 actuator adapter: under option A the lane identity is the worktree→workspace
    mapping, so the j#70250 ``lane_identity_matches`` guard validates against the
    request's own coordinates rather than a tmux label parse.

    ``env`` / ``runner`` are injected so tests drive a fake herdr; the binary is resolved
    from ``env`` (trusted-environment only), exactly like every other herdr path.
    """

    repo_root: Path
    lane_label: str
    issue: str
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS

    def _actuator_ops(self) -> HerdrSublaneActuatorOps:
        return HerdrSublaneActuatorOps(
            repo_root=self.repo_root,
            lane_label=self.lane_label,
            issue=self.issue,
            env=self.env,
            runner=self.runner,
            timeout=self.timeout,
        )

    def worker_provider(self) -> str:
        """The implementer (worker) role's runtime provider from the binding (Redmine #13569).

        Default ``claude`` (byte-identical); a rebound worker provider moves the herdr
        ``--to`` receiver with no source edit. Unbound -> fail-closed zero-send.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            resolve_worker_provider,
        )

        return resolve_worker_provider(str(self.repo_root))

    def command_authority_pins(self) -> dict:
        """The stable-lane authority pins the replayable outcome command must carry (#13485).

        Redmine #13485 review F1: the outcome / dry-run ``command`` is a *replayable retry
        command*, and on the herdr rail the actual dispatch pins ``--target-lane`` (the
        lane the ``read_lane`` decode confirmed) and the #13397 ``--repo`` backend root.
        The use case reads these through ``getattr`` (an optional port capability the tmux
        :class:`LiveWorkerDispatchOps` does not provide) and threads them into
        :func:`_replayable_command`, so the printed / journaled command is byte-identical to
        the argv this adapter actually drove — a safe replay that re-resolves the SAME
        stable slot, never the sender-derived lane. The tmux command carries no pins.
        """
        return {"target_lane": self.lane_label, "repo_root": str(self.repo_root)}

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]:
        """The #13331 live-inventory lane read-back (worktree → workspace → slots)."""
        return self._actuator_ops().read_lane(worktree_path)

    def observe_worker_dispatch_admission(
        self, *, lane: SublaneLaneView, request: WorkerDispatchRequest
    ) -> WorkerDispatchAdmission:
        """Join the current lifecycle, attestation, live receiver and exact delivery."""
        from mozyo_bridge.core.state.herdr_delivery_ledger import HerdrDeliveryLedger
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
            evaluate_attestation,
        )
        from mozyo_bridge.core.state.lane_pin_role import (
            PIN_PAIR_ABSENT,
            read_declared_pin_pair,
        )
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            LaneLifecycleKey,
            ProcessGenerationPin,
            norm,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
            list_herdr_agent_rows,
            repo_scope_workspace_id,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            AGENT_KEY_NAME,
            _agent_locator,
            encode_assigned_name,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
            SLOT_STALE,
            classify_named_slot,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
            agent_row_runtime_state,
        )

        provider = self.worker_provider()
        assigned_name = encode_assigned_name(lane.workspace_id, provider, lane.lane_id)
        rows = list_herdr_agent_rows(self.env)
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get(AGENT_KEY_NAME, "")).strip() == assigned_name
        ]
        row = matches[0] if len(matches) == 1 else None
        locator = _agent_locator(row) if row is not None else ""
        slot_state = classify_named_slot(row) if row is not None else "absent"
        receiver_state = agent_row_runtime_state(row) if row is not None else "absent"

        scope = repo_scope_workspace_id(self.repo_root)
        lifecycle = (
            LaneLifecycleStore().get(LaneLifecycleKey(scope, request.lane_label))
            if scope
            else None
        )
        lifecycle_current = bool(
            lifecycle
            and lifecycle.issue_id == request.issue
            and lifecycle.lane_disposition == "active"
            and lifecycle.lane_generation > 0
        )
        anchor_current = bool(
            lifecycle and lifecycle.decision_journal == (request.journal or "").strip()
        )
        attestation = HerdrIdentityAttestationStore().read(assigned_name)
        joined = evaluate_attestation(
            attestation,
            live_locator=locator,
            expected_workspace_id=lane.workspace_id,
            expected_role=provider,
            expected_lane=lane.lane_id,
        )
        declared_pair = read_declared_pin_pair(lifecycle) if lifecycle else None
        worker_pin = declared_pair.worker if declared_pair and declared_pair.ok else None
        declared_reason = declared_pair.reason if declared_pair else PIN_PAIR_ABSENT
        # A create-path lane (`sublane create --no-dispatch`) declares its owner row through
        # `declare_active`, which writes NO declared-slot snapshot — a legitimate fresh
        # generation-1 shape (Redmine #13846 R4, live evidence #14062 j#82028). ONLY a
        # genuinely slot-less row (`PIN_PAIR_ABSENT`) may fall back to the startup
        # self-attestation authority below; a positively suspicious declared shape (foreign /
        # mixed / duplicate / incomplete / unreadable) is never proof of the current
        # generation and stays fail-closed — "the row has pins" is not itself the proof.
        declared_slots_absent = declared_reason == PIN_PAIR_ABSENT
        declared_identity_current = bool(
            worker_pin
            and norm(worker_pin.provider) == norm(provider)
            and norm(worker_pin.assigned_name) == norm(assigned_name)
        )
        declared_attestation_current = False
        if declared_identity_current and attestation is not None:
            declared_attestation_current = bool(
                norm(getattr(attestation, "assigned_name", "")) == norm(assigned_name)
                and evaluate_attestation(
                    attestation,
                    live_locator=worker_pin.locator,
                    expected_workspace_id=lane.workspace_id,
                    expected_role=provider,
                    expected_lane=lane.lane_id,
                ).ok
            )

        live_generation_current = False
        if declared_identity_current and row is not None and locator:
            try:
                live_pin = ProcessGenerationPin(
                    # The slot label is declaration authority; a live row does not expose it.
                    role=worker_pin.role,
                    provider=norm(row.get("provider")) or norm(provider),
                    assigned_name=assigned_name,
                    locator=locator,
                    runtime_revision=norm(row.get("runtime_revision")),
                )
            except (TypeError, ValueError):
                live_generation_current = False
            else:
                # Redmine #13846: bind on the (role/provider/assigned_name/locator) identity,
                # treating runtime_revision as supplementary evidence. A full match_key equality
                # rejects a current fresh generation whose declared pin never observed a runtime
                # version while the live `agent list` row surfaces one (locator still matches) —
                # the false `worker_liveness_authority_conflict`. A same-name process re-launched
                # at a newer revision (both observed, differ) or a locator drift still fails closed.
                live_generation_current = worker_pin.binds_same_generation(live_pin)

        # The slot-less create-path generation authority (Redmine #13846 R4): with no declared
        # worker pin, the live worker's startup self-attestation generation-bound to the LIVE
        # locator is the herdr generation discriminant (`herdr-native-identity.md`: the
        # discriminant is the live locator; the attestation store records no runtime version).
        # ``joined`` (above) already proves the attestation is present, identity-matched
        # (workspace/role/lane) and locator-current; the assigned_name is verified explicitly,
        # exactly as the declared path does. A slot-less row whose live worker is absent,
        # unattested, or stale (a drifted locator) has no such authority and fails closed.
        #
        # Redmine #13846 R4 review F1: the slot-less path must ALSO reject a wrong live provider,
        # exactly as the declared path does through ``binds_same_generation`` (``live_pin.provider``).
        # The live row surfaces the bound provider two ways — its ``provider`` field and its
        # detected-agent field (``agent``, which on a live pane holds the provider id). A row whose
        # surfaced provider / detected agent disagrees with the resolved worker provider is a
        # foreign / mis-bound process even when the name and locator line up, so it is never this
        # generation's worker (provider is an identity-authority field). An UNsurfaced field falls
        # back to the name-encoded provider — the declared path's ``... or norm(provider)`` shape —
        # never fabricating a match.
        want_provider = norm(provider)
        live_row_provider = norm(row.get("provider")) if row is not None else ""
        live_detected_agent = norm(row.get("agent")) if row is not None else ""
        fresh_live_provider_consistent = (
            (not live_row_provider or live_row_provider == want_provider)
            and (not live_detected_agent or live_detected_agent == want_provider)
        )
        fresh_attested_generation_current = bool(
            declared_slots_absent
            and row is not None
            and locator
            and joined.ok
            and norm(getattr(attestation, "assigned_name", "")) == norm(assigned_name)
            and fresh_live_provider_consistent
        )

        # The action-time process-generation binding, from whichever authority the
        # declaration surface actually provided. A live receiver must match the declared pin's
        # process generation; for an absent receiver on the declared path the stored
        # self-attestation proves which declared generation is absent without fabricating live
        # presence. A slot-less create binds on the live startup self-attestation. Every other
        # shape (a suspicious declared set, or a slot-less row without a live attested worker)
        # is not the current generation and fails closed.
        if worker_pin is not None:
            generation_binding_current = (
                (live_generation_current and declared_attestation_current)
                if locator else declared_attestation_current
            )
        else:
            generation_binding_current = fresh_attested_generation_current
        generation_binding_detail = _generation_binding_detail(
            current=generation_binding_current,
            worker_pin=worker_pin,
            declared_slots_absent=declared_slots_absent,
            declared_reason=declared_reason,
            declared_identity_current=declared_identity_current,
            declared_attestation_current=declared_attestation_current,
            live_generation_current=live_generation_current,
            live_present=bool(locator),
            fresh_live_provider_consistent=fresh_live_provider_consistent,
        )
        lifecycle_action = norm(
            getattr(lifecycle, "replacement_action_id", "") if lifecycle else ""
        )
        attestation_action = norm(
            getattr(attestation, "replacement_action_id", "")
            if attestation is not None
            else ""
        )
        if lifecycle_action or attestation_action:
            action_binding_current = bool(
                declared_attestation_current
                and lifecycle_action
                and lifecycle_action == attestation_action
            )
        else:
            # A normal launch has no replacement transaction. Its exact declared process
            # pin is generation-bound authority; empty strings alone are not authority.
            action_binding_current = generation_binding_current
        duplicate = any(
            record.journal_id == (request.journal or "").strip()
            and (record.receiver == provider or record.provider == provider)
            and record.target in {assigned_name, locator}
            for record in HerdrDeliveryLedger().records_for_issue(request.issue)
        )
        terminal_absence = bool(
            (len(matches) == 0 and lifecycle_current and declared_identity_current)
            or (
                len(matches) == 1
                and slot_state == SLOT_STALE
                and not locator
                and lifecycle_current
                and declared_identity_current
            )
        )
        facts = WorkerDispatchAdmissionFacts(
            lifecycle_current=lifecycle_current,
            anchor_current=anchor_current,
            identity_attested=joined.ok,
            action_binding_current=action_binding_current,
            slot_state=(slot_state if len(matches) == 1 else "ambiguous"),
            locator_present=bool(locator),
            receiver_state=receiver_state,
            generation_binding_current=generation_binding_current,
            generation_binding_detail=generation_binding_detail,
            terminal_absence_authoritative=terminal_absence,
            duplicate_or_uncertain_delivery=duplicate,
            workspace_id=scope or None,
            lane_id=request.lane_label,
            lane_generation=(lifecycle.lane_generation if lifecycle else None),
            worker_assigned_name=assigned_name,
            worker_locator=locator or None,
            action_id=(
                lifecycle_action
                if lifecycle_action
                else (
                    f"lane_generation_{lifecycle.lane_generation}"
                    if lifecycle and generation_binding_current
                    else None
                )
            ),
        )
        return decide_worker_dispatch_admission(facts)

    def observe_sender_admission(
        self,
        *,
        lane: SublaneLaneView,
        request: WorkerDispatchRequest,
        allow_direct_worker: bool = False,
    ) -> SenderDispatchAdmission:
        """Pre-reserve verdict: is the SENDER this lane's current same-lane gateway? (#14192)

        Resolves the sender's launch-time :class:`SenderIdentity` from ``self.env``
        (``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID`` cross-checked
        against the repo anchor) EXACTLY as the inner ``handoff send`` rail does
        (``herdr_send_entry.resolve_herdr_send_target`` — the shared
        ``herdr_workspace_segment`` anchor + the pre-#13377 legacy-lane fallback), then runs
        the SAME pure :func:`decide_gateway_route` policy the inner rail enforces. Because the
        inputs and the policy are identical, an admitted sender here is exactly one the inner
        rail's gateway-route gate would also admit — so the preflight never over-blocks a
        legitimate same-lane gateway (Acceptance #4), while a coordinator / foreign /
        cross-lane sender (or an unattested origin) fails closed BEFORE any outbox reserve
        with zero write and zero send (Acceptance #1). The verdict is a pure function of the
        sender env + resolved lane, so it is identical on dry-run and execute (Acceptance #2).
        An explicit ``--allow-direct-worker`` releases a cross-lane drive as an exception,
        mirroring the inner rail's ``gateway_route_exception``.
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
            herdr_workspace_segment,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (  # noqa: E501
            _legacy_lane_token,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
            MOZYO_WORKSPACE_ID_ENV,
            resolve_sender_identity,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            WorkflowProviderUnresolved,
            resolve_gateway_provider,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_route_enforcement import (  # noqa: E501
            GatewayRouteRequest,
            decide_gateway_route,
        )

        # Anchor workspace: the shared project segment, with the pre-#13377 legacy per-lane
        # token accepted when a live legacy lane's env still carries it (mirrors
        # herdr_send_entry so this preflight and the inner rail resolve the SAME sender).
        anchor_ws = herdr_workspace_segment(self.repo_root) or None
        env_ws = (self.env.get(MOZYO_WORKSPACE_ID_ENV) or "").strip()
        if env_ws and env_ws != (anchor_ws or ""):
            legacy_token = _legacy_lane_token(self.repo_root)
            if legacy_token and env_ws == legacy_token:
                anchor_ws = legacy_token

        sender_res = resolve_sender_identity(self.env, anchor_workspace_id=anchor_ws)
        if not sender_res.ok or sender_res.identity is None:
            return SenderDispatchAdmission(
                admitted=False,
                reason=(
                    "dispatch-worker sender is not an attested same-lane gateway "
                    f"({sender_res.reason}); refusing to reserve the outbox or send from a "
                    "foreign / unattested origin. Dispatch through the coordinator -> "
                    "target-lane gateway -> same-lane worker route instead"
                ),
                detail_token=sender_res.reason or "sender_identity_unresolved",
            )

        try:
            worker_provider = self.worker_provider()
            gateway_provider = resolve_gateway_provider(str(self.repo_root))
        except WorkflowProviderUnresolved as exc:
            return SenderDispatchAdmission(
                admitted=False,
                reason=f"worker/gateway provider binding is unresolved: {exc}",
                detail_token="provider_binding_unresolved",
            )

        sender = sender_res.identity
        decision = decide_gateway_route(
            GatewayRouteRequest(
                kind="implementation_request",
                receiver=worker_provider,
                sender_identity_known=True,
                sender_workspace_id=sender.workspace_id,
                sender_lane_id=sender.lane_id,
                # The cross-workspace enforcement is already done by the anchor cross-check in
                # `resolve_sender_identity` (env workspace == repo anchor == the target lane's
                # workspace, since `read_lane` resolves the SAME `self.repo_root`), exactly as
                # the inner rail enforces it. Passing the resolved sender workspace keeps the
                # route decision's discriminator the LANE alone, so a token disagreement between
                # `herdr_workspace_segment` and the `read_lane` projection can never over-block a
                # legitimate same-lane gateway (Acceptance #4). A foreign-workspace sender has
                # already failed closed at identity resolution above.
                target_workspace_id=sender.workspace_id,
                # The `--target-lane` pin the dispatch drives is the request label, which is
                # exactly the explicit lane the inner rail's route target resolves to (tier-1).
                target_lane_id=request.lane_label,
                allow_direct_worker=allow_direct_worker,
                worker_provider=worker_provider,
                gateway_provider=gateway_provider,
            )
        )
        if decision.is_blocked:
            return SenderDispatchAdmission(
                admitted=False,
                reason=(
                    "dispatch-worker sender lane does not match the target lane's same-lane "
                    "gateway route; fail-closed before any outbox reserve. Route through the "
                    "target lane's gateway, or supply --allow-direct-worker for an explicit "
                    "durable cross-lane exception"
                ),
                detail_token=decision.blocked_reason or "gateway_route_blocked",
            )
        return SenderDispatchAdmission(
            admitted=True,
            reason="sender is the target lane's current same-lane gateway route",
            exception_applied=decision.is_exception,
        )

    def probe_worker_ready(self, worker_pane: str) -> bool:
        """One non-fatal live-presence snapshot of the worker locator (#13301 herdr form).

        Delegates to the #13331 presence probe — a role-agnostic "is this locator live in
        the inventory now" check — because a server-spawned herdr agent has no TUI
        boot/render race to observe and the send rail's #13322 self-healing is the landing
        net. Any read failure returns ``False`` (never fatal); the use case polls this on
        its bounded window exactly as it does the tmux probe.
        """
        return self._actuator_ops().probe_gateway_ready(worker_pane)

    @staticmethod
    def _fence_key(admission: WorkerDispatchAdmission, request: WorkerDispatchRequest):
        from mozyo_bridge.core.state.dispatch_outbox_fence import FenceKey

        facts = admission.facts
        required = (
            facts.workspace_id,
            facts.lane_id,
            request.issue,
            request.journal,
            facts.action_id,
            facts.worker_assigned_name,
        )
        if not admission.is_healthy or not all(required):
            return None
        return FenceKey(*(str(value) for value in required))

    def reserve_worker_dispatch(
        self, *, admission: WorkerDispatchAdmission, request: WorkerDispatchRequest
    ) -> tuple[bool, str]:
        """Atomically reserve the sole exact send before injection."""
        from mozyo_bridge.core.state.dispatch_outbox_fence import (
            DispatchOutboxFence,
            DispatchOutboxFenceError,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (
            target_is_retiring,
        )

        key = self._fence_key(admission, request)
        if key is None:
            return False, "incomplete deterministic fence key"
        fence = DispatchOutboxFence()
        try:
            fence.bootstrap()
            reservation = fence.reserve(key)
            if not reservation.won:
                return False, (
                    f"exact send key already {reservation.current_state}; "
                    "prior injection is never replayed"
                )
            retiring, detail = target_is_retiring(key.target_assigned_name)
            if retiring:
                fence.mark_cancelled(key, detail=detail)
                return False, detail
        except DispatchOutboxFenceError as exc:
            return False, f"dispatch outbox authority unavailable: {exc}"
        except Exception as exc:  # noqa: BLE001 - a reserved pre-send key stays never-send
            return False, f"dispatch retirement/fence preflight failed: {type(exc).__name__}"
        return True, reservation.current_state

    def complete_worker_dispatch(
        self,
        *,
        admission: WorkerDispatchAdmission,
        request: WorkerDispatchRequest,
        delivered: bool,
        detail: str,
        known_not_sent: bool = False,
    ) -> bool:
        """Persist delivered only for turn-start; every other send is uncertain.

        Redmine #14192: a ``known_not_sent`` non-delivered outcome — the inner rail
        PROVED a pre-injection zero-send (``gateway_route_blocked`` /
        ``reader_upgrade_required``, text / Enter 0) — is CANCELLED (a never-replay
        terminal that honestly records "not sent"), NOT poisoned to the reconcile-only
        ``uncertain`` terminal. Every other non-delivered outcome — an unparseable
        record, a timeout, or a post-injection failure whose fate is unknown — stays
        ``uncertain`` and never-replay, byte-for-byte the prior behaviour. The exactly-once
        reserve-before-send invariant is unchanged: both terminals are never-send.
        """
        from mozyo_bridge.core.state.dispatch_outbox_fence import (
            DispatchOutboxFence,
            DispatchOutboxFenceError,
        )

        key = self._fence_key(admission, request)
        if key is None:
            return False
        fence = DispatchOutboxFence()
        try:
            if delivered and fence.mark_delivered(key, detail=detail):
                return True
            if not delivered and known_not_sent:
                fence.mark_cancelled(
                    key,
                    detail=detail
                    or "inner rail proved known-not-sent (zero injection) before send",
                )
            else:
                fence.record_uncertain(
                    key, detail=detail or "worker send outcome uncertain"
                )
        except DispatchOutboxFenceError:
            return False
        except Exception:  # noqa: BLE001 - an unconfirmed outcome is never delivered
            return False
        return not delivered

    def dispatch_to_worker(
        self,
        *,
        issue: str,
        journal: str,
        worker_pane: str,
        lane_label: str,
        gateway_callback_target: Optional[str],
        target_repo: str,
        allow_direct_worker: bool = False,
    ) -> int:
        """Drive the governed same-lane worker forward on the herdr rail (measured ACK).

        The argv the tmux adapter composes, plus two herdr-only pins (``--repo`` /
        ``--target-lane``): ``worker_pane`` is a live herdr locator (never ``%N``), so the
        #13320 effective-backend predicate routes the send onto the herdr rail, where
        ``--target-repo auto`` resolves to the sender's own repo root (#13331 j#73312 #2 —
        the same-workspace worker's repo) and the queue-enter rail submit-completes with
        the #13322 turn-start observation + Enter-resend self-healing. The exit code —
        contained by the shared j#71597 helper — is the delivery-ACK measurement the use
        case promotes (0) or fails closed on (non-0, ``gateway_notified`` kept). Calls
        resolve through the dispatcher module attribute so its established monkeypatch
        seams keep working.

        Redmine #13485: the herdr rail re-resolves its target through the #13305
        backend-neutral route authority, which discards the ``worker_pane`` locator and
        derives the target lane. Passing ``target_lane=lane_label`` pins that lane to the
        stable ``(workspace, lane_label, claude)`` identity the ``read_lane`` inventory
        decode already confirmed, so the ACK measures submit-completion to the intended
        worker even when the SENDER's launch-time lane attestation diverges (a coordinator
        / cross-lane stall-drive, or a legacy gateway) — the send no longer silently
        ACKs on a different / stale ``claude`` while the real lane worker stays idle
        (#13483 j#74570). This mirrors the coordinator→gateway leg, which already pins
        ``--target-lane`` (:meth:`HerdrSublaneActuatorOps.dispatch_argv`).
        """
        rc, _known_not_sent = _worker_dispatcher._drive_worker_send_argv(
            self._compose_worker_send_argv(
                issue=issue,
                journal=journal,
                worker_pane=worker_pane,
                lane_label=lane_label,
                gateway_callback_target=gateway_callback_target,
                target_repo=target_repo,
                allow_direct_worker=allow_direct_worker,
            )
        )
        return rc

    def _compose_worker_send_argv(
        self,
        *,
        issue: str,
        journal: str,
        worker_pane: str,
        lane_label: str,
        gateway_callback_target: Optional[str],
        target_repo: str,
        allow_direct_worker: bool = False,
    ) -> list[str]:
        """The governed same-lane forward argv with the herdr-only pins (shared).

        Composed once so both :meth:`dispatch_to_worker` (rc only) and
        :meth:`dispatch_to_worker_turn_start` (rc + turn-start + the #14192
        ``known_not_sent`` classification) drive the byte-identical argv.
        """
        return _worker_dispatcher._worker_dispatch_argv(
            issue=issue,
            journal=journal,
            worker_pane=worker_pane,
            lane_label=lane_label,
            gateway_callback_target=gateway_callback_target,
            target_repo=target_repo,
            allow_direct_worker=allow_direct_worker,
            # Redmine #13485: pin the worker's stable lane identity so the herdr
            # route authority resolves `(workspace, lane_label, claude)` explicitly,
            # not the sender-derived lane (tier-2). The tmux adapter omits this
            # (default None) and its `%pane` target never rides the lane rail.
            target_lane=lane_label,
            # Redmine #13397: pin the inner send's effective-backend resolution to the
            # SAME repo the outer `sublane dispatch-worker` selected herdr on
            # (`self.repo_root` — the value `repo_backend_is_herdr` returned True for),
            # not the driving process's cwd. Without this, an external adopted project
            # (whose `backend: herdr` selection lives only at the adopted root, not a
            # committed config every checkout carries) re-derives `backend: tmux` from a
            # divergent cwd and validates the herdr worker locator (`worker_pane`, a
            # non-`%pane` handle) as an invalid tmux target — the #13379 j#73722 blocker.
            repo_root=str(self.repo_root),
            # Redmine #13569: the `--to` receiver is the binding-resolved worker provider
            # (default `claude`), so a rebound worker follows without a literal edit.
            worker_provider=self.worker_provider(),
        )

    def dispatch_to_worker_turn_start(
        self,
        *,
        issue: str,
        journal: str,
        worker_pane: str,
        lane_label: str,
        gateway_callback_target: Optional[str],
        target_repo: str,
        worker_assigned_name: str,
        allow_direct_worker: bool = False,
    ) -> tuple[int, str, bool]:
        """Drive the worker forward AND surface the herdr turn-start signal (Redmine #13489 F2).

        Returns ``(delivery_ack_rc, turn_start_token, known_not_sent)``. The ACK rc is
        the submit-completion measurement — which is **not** a turn-start confirmation
        (mid-review j#75047 F2). The turn-start token is the dispatch-ops-surfaced herdr
        runtime signal that the receiver's turn actually started: after a positive ACK, the
        exact worker is re-resolved in the live inventory and its runtime receiver-state is
        read — ``busy`` / ``working`` (the turn started) -> ``started``; a still-
        ``awaiting_input`` worker (ACK landed but no turn) -> ``delivered_not_started``; any
        other / unobservable state -> ``unknown``. A non-zero ACK -> ``not_started``.
        Conservative: only a definitive ``started`` promotes to ``delivered`` upstream;
        everything else is uncertain. No raw wait loop is introduced — a single structured
        observation.

        Redmine #14192: ``known_not_sent`` is ``True`` only when a non-zero send's inner
        rail PROVED a pre-injection zero-send (``classify_send_known_not_sent`` over the
        captured structured outcome), so the use case cancels the exact fence key instead
        of poisoning it to ``uncertain``. A zero ACK (delivered) is never a known-not-sent.
        """
        rc, known_not_sent = _worker_dispatcher._drive_worker_send_argv(
            self._compose_worker_send_argv(
                issue=issue,
                journal=journal,
                worker_pane=worker_pane,
                lane_label=lane_label,
                gateway_callback_target=gateway_callback_target,
                target_repo=target_repo,
                allow_direct_worker=allow_direct_worker,
            )
        )
        if int(rc or 0) != 0:
            return rc, "not_started", known_not_sent
        return (
            rc,
            self._observe_worker_turn_start(
                worker_assigned_name,
                issue=issue,
                journal=journal,
                worker_locator=worker_pane,
            ),
            known_not_sent,
        )

    def _observe_worker_turn_start(
        self,
        worker_assigned_name: str,
        *,
        issue: str,
        journal: str,
        worker_locator: str,
    ) -> str:
        """Read exact ledger event/queue telemetry; never take a fresh runtime snapshot."""
        from mozyo_bridge.core.state.herdr_delivery_ledger import HerdrDeliveryLedger

        try:
            records = HerdrDeliveryLedger().records_for_issue(issue)
        except Exception:  # noqa: BLE001 - unobservable telemetry is unknown
            return "unknown"
        exact = [
            record
            for record in records
            if record.journal_id == journal
            and (
                record.receiver == self.worker_provider()
                or record.provider == self.worker_provider()
            )
            and record.target in {worker_assigned_name, worker_locator}
        ]
        for record in reversed(exact):
            event = record.turn_start_outcome or {}
            event_token = str(event.get("outcome", "")).strip()
            if event_token in {"started", "delivered_not_started"}:
                return event_token
            queue = record.queue_enter_observation or {}
            runtime = str(queue.get("runtime_state", "")).strip()
            if runtime in {"busy", "working"}:
                return "started"
            if runtime == "awaiting_input":
                return "delivered_not_started"
        return "unknown"


__all__ = ("HerdrWorkerDispatchOps",)
