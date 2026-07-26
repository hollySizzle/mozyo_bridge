"""Live adapter for the hibernated exact-pair recovery (Redmine #13847 items 3/4/5).

Wires the pure use case (:mod:`sublane_hibernated_pair_recovery`) to the real runtime,
REUSING already-reviewed live machinery so it adds no new low-level transaction core:

- **observe** — the live herdr inventory + slot-liveness + the #13637 startup attestation +
  a lifecycle re-read (the action-time newer-generation fence) + the #13763 quarantine
  pending-composer inspection, joined into the pure per-slot :class:`SlotRecoveryObservation`;
- **close** — the #13763 :class:`LiveSublaneQuarantineOps.close_receiver`, pin-matched to the
  exact LIVE bad-generation locator (byte-preserving; a same-name recycle at a new locator is
  never closed — the exact old slot is absent);
- **relaunch** — the herdr actuator :meth:`heal_lane_column` (adopt-or-launch idempotent per
  slot: the healthy slot is adopted, only the closed slot relaunches);
- **redispatch** — the existing :class:`DispatchOutboxFence` as the sole exactly-once
  authority, then the governed coordinator->gateway ``dispatch_implementation_request``. A
  delivery ACK is never promoted to task start / completion (item 5).

Consistent with the boundary (no dogfood actuation during the request), the adapter is
exercised by isolated tests with a fake herdr runner / isolated stores — it never needs a
real managed pair. The destructive effects still fail closed: an unreadable inventory /
lifecycle / attestation is never degraded to a positive pass, and a redispatch is fenced
before the send so a replay never re-delivers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    DispatchOutboxFenceError,
    FENCE_DELIVERED,
    FENCE_RESERVED,
    FENCE_UNCERTAIN,
    FenceKey,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    BINDING_BOUND,
    HerdrIdentityReplacementBindingStore,
    replacement_action_is_bound,
    selected_attestation_store_is_v1,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ReleasePin,
    ReleasePinError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    probe_worktree_resolved,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
    REDISPATCH_FAILED,
    REDISPATCH_TARGET_RETIRING,
    REDISPATCH_UNCERTAIN,
    HibernatedPairRecoveryOps,
    SlotPlan,
    SublaneRecoverPairDeliveryUseCase,
    SublaneRecoverPairUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    KIND_IMPLEMENTATION_REQUEST,
    RecoveryAnchorDeliveryRequest,
    parse_recovery_delivery_authorizations,
    parse_recovery_delivery_zero_send_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
    LiveSublaneQuarantineOps,
    QuarantineRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E501
    LiveSublaneResumeOps,
    SublaneResumeUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E501
    SlotRecoveryObservation,
    hibernated_pair_recovery_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneLauncherIncompatibleError,
    SublaneStartupObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    SublaneHealError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_BUSY,
    map_agent_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.shared.paths import mozyo_bridge_home

_STATUS_KEYS = ("agent_status", "status", "state")


def _row_runtime_state(row: Mapping[str, object]) -> str:
    for key in _STATUS_KEYS:
        if key in row:
            return map_agent_status(row.get(key))
    return ""


@dataclass
class LiveHibernatedPairRecoveryOps:
    """Live :class:`HibernatedPairRecoveryOps`: observe / close bad gen / relaunch / redispatch."""

    repo_root: Path
    request_issue: str
    request_lane: str
    request_journal: str
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS
    #: Injectable store homes so tests drive isolated state (default = the real home).
    lifecycle_home: Optional[Path] = None
    attestation_home: Optional[Path] = None
    fence: Optional[DispatchOutboxFence] = None
    relaunch_failure_reason: str = field(default="", init=False)
    relaunch_failure_startup: Optional[SublaneStartupObservation] = field(
        default=None, init=False
    )

    # -- workspace ---------------------------------------------------------------------

    def workspace_id(self) -> str:
        try:
            return repo_scope_workspace_id(self.repo_root)
        except Exception:  # noqa: BLE001 - unresolved workspace => empty (fail closed upstream)
            return ""

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return list_herdr_agent_rows(self.env)

    def _quarantine(self) -> LiveSublaneQuarantineOps:
        # Redmine #14065 Phase 2: inject the dim-ghost render policy so the converge /
        # recover slot-recovery decision (and its action-time re-observation) empties a
        # provider ghost idle placeholder while preserving real unsent input.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_ghost_composer_observation import (  # noqa: E501
            default_ghost_policy,
        )

        return LiveSublaneQuarantineOps(
            repo_root=self.repo_root,
            env=self.env,
            runner=self.runner,
            timeout=self.timeout,
            ghost_policy=default_ghost_policy(),
        )

    def _quarantine_request(self, *, role: str, assigned_name: str, locator: str, action_id: str) -> QuarantineRequest:
        return QuarantineRequest(
            issue=_norm(self.request_issue),
            lane=_norm_lane(self.request_lane),
            journal=_norm(self.request_journal),
            role=_norm(role),
            assigned_name=_norm(assigned_name),
            locator=_norm(locator),
            action_generation=_norm(action_id),
            approval_observed_at="",
            approved_revision=-1,
        )

    # -- observe -----------------------------------------------------------------------

    def observe_slot(
        self, *, role: str, provider: str, workspace_id: str, lane: str, record: Any
    ) -> Tuple[SlotRecoveryObservation, str, str]:
        assigned_name = encode_assigned_name(workspace_id, provider, lane)
        try:
            rows = self._rows()
        except Exception:  # noqa: BLE001 - UNREADABLE inventory => nothing observable (preserve)
            return SlotRecoveryObservation(), "", assigned_name
        matches = [
            row for row in rows
            if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME)) == _norm(assigned_name)
        ]
        if len(matches) == 0:
            # A VANISHED pair slot (0 live panes — e.g. closed in a prior partial run): relaunch-
            # recoverable, unless the lane generation was superseded (the newer fence still
            # applies to an absent slot). Distinct from an UNREADABLE inventory above (Redmine
            # #13847 R1-F1). No live locator to pin — the relaunch recreates it.
            return (
                SlotRecoveryObservation(
                    slot_absent=True,
                    generation_not_newer=self._generation_not_newer(record, workspace_id, lane),
                ),
                "",
                assigned_name,
            )
        if len(matches) != 1:
            # ambiguous (a duplicate name) => not resolved, not absent => preserve.
            return SlotRecoveryObservation(), "", assigned_name
        row = matches[0]
        live_locator = _agent_locator(row)
        if not live_locator:
            return SlotRecoveryObservation(), "", assigned_name
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        belongs = bool(
            decoded.ok
            and decoded.identity is not None
            and decoded.identity.workspace_id == _norm(workspace_id)
            and _norm_lane(decoded.identity.lane_id) == _norm_lane(lane)
            and decoded.identity.role == _norm(provider)
        )
        # attestation join at the LIVE locator: attested => already healthy; present but
        # not attested (absent / stale / missing / conflict) => the bad generation to close.
        # A store READ ERROR (Redmine #13847 R1-F4) is NOT a positive bad-generation fact:
        # `att_readable` gates BOTH `is_bad_generation` and `already_healthy`, so an unreadable
        # attestation store leaves the slot indeterminate -> preserve (zero-close), never close.
        record_att, att_readable = self._read_attestation(assigned_name)
        join = evaluate_attestation(
            record_att,
            live_locator=live_locator,
            expected_workspace_id=workspace_id,
            expected_role=provider,
            expected_lane=lane,
        )
        # A normal-v1 self-attestation is insufficient after this recovery action has
        # reserved a replacement.  The side binding and its startup transaction are the
        # authority that prevents a fresh-but-unbound / rollback-owed participant from
        # being promoted to healthy on replay.  A reserved, malformed, or unreadable binding
        # is therefore PRESERVE (neither bad-generation nor healthy): the operator must
        # discharge the exact startup rollback debt before the same action can relaunch.
        binding_allows_classification = True
        home = self.attestation_home or mozyo_bridge_home()
        try:
            is_v1 = selected_attestation_store_is_v1(home)
        except Exception:  # noqa: BLE001 - unknown generation is not positive health
            is_v1 = True
            binding_allows_classification = False
        if is_v1 and binding_allows_classification:
            try:
                recovery_action_id = hibernated_pair_recovery_action_id(
                    issue=_norm(self.request_issue),
                    lane_id=_norm_lane(lane),
                    revision=str(getattr(record, "revision", "")),
                    generation=str(getattr(record, "lane_generation", "")),
                )
                binding = HerdrIdentityReplacementBindingStore(home=home).read(
                    recovery_action_id, assigned_name
                )
            except Exception:  # noqa: BLE001 - unreadable authority preserves the live slot
                binding_allows_classification = False
            else:
                if binding is not None:
                    binding_allows_classification = bool(
                        binding.phase == BINDING_BOUND
                        and replacement_action_is_bound(
                            record_att,
                            action_id=recovery_action_id,
                            live_locator=live_locator,
                            expected_workspace_id=workspace_id,
                            expected_role=provider,
                            expected_lane=lane,
                            expected_assigned_name=assigned_name,
                            expected_old_locator=binding.old_locator,
                            home=home,
                        )
                    )
        observation = SlotRecoveryObservation(
            identity_resolved=True,
            belongs_to_pair=belongs,
            generation_not_newer=self._generation_not_newer(record, workspace_id, lane),
            not_productive=_row_runtime_state(row) != RUNTIME_BUSY,
            no_pending_composer=self._no_pending_composer(
                role=role, assigned_name=assigned_name, locator=live_locator
            ),
            worktree_readable=self._worktree_readable(row),
            is_bad_generation=(
                belongs
                and att_readable
                and binding_allows_classification
                and not join.ok
            ),
            already_healthy=(
                att_readable and binding_allows_classification and join.ok
            ),
        )
        return observation, live_locator, assigned_name

    def _read_attestation(self, assigned_name: str) -> "Tuple[Any, bool]":
        """Return ``(record, readable)``: the slot's self-attestation and whether the store
        READ succeeded (Redmine #13847 R1-F4).

        A genuinely-absent record (store readable, no row) is ``(None, True)`` — the live-but-
        unattested residue the recovery closes. A store READ ERROR is ``(None, False)`` — the
        caller must NOT treat that as a bad generation (it is unknowable, so fail closed to
        preserve). The two must never be conflated.
        """
        try:
            record = HerdrIdentityAttestationStore(home=self.attestation_home).read(_norm(assigned_name))
        except Exception:  # noqa: BLE001 - unreadable attestation store => (None, not readable)
            return None, False
        return record, True

    def _generation_not_newer(self, record: Any, workspace_id: str, lane: str) -> bool:
        """Re-read the live lifecycle: the pinned generation must still be the current one.

        A concurrent transition / newer generation bumps the row ``revision`` (or leaves the
        lane no longer ``hibernated``); either means the approval the recovery pins is stale,
        so the slot is preserved (zero-close). An unreadable / absent lifecycle fails closed.
        """
        pinned_rev = _norm(getattr(record, "revision", ""))
        if not pinned_rev:
            return False
        try:
            live = LaneLifecycleStore(home=self.lifecycle_home).get(
                LaneLifecycleKey(_norm(workspace_id), _norm_lane(lane))
            )
        except (LaneLifecycleError, ValueError, OSError):
            return False
        return bool(
            live is not None
            and live.lane_disposition == DISPOSITION_HIBERNATED
            and _norm(live.revision) == pinned_rev
        )

    def _no_pending_composer(self, *, role: str, assigned_name: str, locator: str) -> bool:
        """No pending (unsent) composer input on the slot (fail-closed on any doubt).

        Reuses the #13763 quarantine inspection's RAW composer signal — not its
        classification, which is purpose-specific and short-circuits ``IDENTITY_UNATTESTED``
        on exactly the unattested slots this recovery targets. Only a positively NON-pending
        composer (``signal.has_pending is False``) clears the gate; a pending (``True``) or
        unknown (``None`` / uninspectable) composer preserves the slot so a close never drops
        un-sent input.
        """
        try:
            inspection = self._quarantine().inspect(
                self._quarantine_request(role=role, assigned_name=assigned_name, locator=locator, action_id="")
            )
        except Exception:  # noqa: BLE001 - uninspectable composer => preserve (fail closed)
            return False
        return inspection.signal.has_pending is False

    def _worktree_readable(self, row: Mapping[str, object]) -> bool:
        raw = _norm(row.get("foreground_cwd") or row.get("cwd"))
        if not raw:
            return False
        try:
            return probe_worktree_resolved(str(raw)) is True
        except Exception:  # noqa: BLE001 - unreadable worktree fails closed
            return False

    # -- close (byte-preserving, exact live locator) -----------------------------------

    def close_bad_slot(
        self, *, role: str, provider: str, assigned_name: str, locator: str, action_id: str
    ) -> bool:
        try:
            release = ReleasePin(role=_norm(provider), assigned_name=_norm(assigned_name), locator=_norm(locator))
        except ReleasePinError:
            return False
        try:
            result = self._quarantine().close_receiver(
                self._quarantine_request(role=role, assigned_name=assigned_name, locator=locator, action_id=action_id),
                release,
            )
        except Exception:  # noqa: BLE001 - a fixed close failure, nothing partially closed
            return False
        # A positively-absent exact slot (recycled / already gone) is byte-preserving: not an
        # error — the relaunch recreates it. A real close failure returns False -> blocked.
        return bool(result.closed or result.old_absent)

    # -- relaunch (heal: adopt healthy, relaunch closed) -------------------------------

    def relaunch_pair(self, *, action_id: str, slots: Tuple[SlotPlan, ...]) -> bool:
        self.relaunch_failure_reason = ""
        self.relaunch_failure_startup = None
        try:
            if selected_attestation_store_is_v1(mozyo_bridge_home()):
                for slot in slots:
                    if not (
                        _norm(slot.provider)
                        and _norm(slot.assigned_name)
                        and _norm(slot.declared_locator)
                    ):
                        self.relaunch_failure_reason = (
                            "replacement_binding_context_missing"
                        )
                        return False
                    HerdrSublaneActuatorOps(
                        repo_root=self.repo_root, lane_label=_norm(self.request_lane),
                        issue=_norm(self.request_issue), journal=_norm(self.request_journal),
                        env=self.env, runner=self.runner, timeout=self.timeout,
                        replacement_action_id=_norm(action_id),
                        replacement_assigned_name=_norm(slot.assigned_name),
                        replacement_old_locator=_norm(slot.declared_locator),
                        replacement_target_only=True,
                    ).heal_lane_column(
                        str(self.repo_root), target_provider=_norm(slot.provider)
                    )
            else:
                HerdrSublaneActuatorOps(
                    repo_root=self.repo_root, lane_label=_norm(self.request_lane),
                    issue=_norm(self.request_issue), journal=_norm(self.request_journal),
                    env=self.env, runner=self.runner, timeout=self.timeout,
                    replacement_action_id=_norm(action_id),
                ).heal_lane_column(str(self.repo_root))
        except SublaneHealError as exc:
            self.relaunch_failure_reason = _norm(exc.reason) or "launch_error"
            self.relaunch_failure_startup = exc.startup
            return False
        except SublaneLauncherIncompatibleError as exc:
            self.relaunch_failure_reason = _norm(exc.reason) or "launch_error"
            return False
        except Exception:  # noqa: BLE001 - a fixed relaunch failure
            self.relaunch_failure_reason = "launch_error"
            return False
        return True

    # -- redispatch (existing outbox fence = sole exactly-once authority, item 5) -------

    def _fence(self) -> DispatchOutboxFence:
        # Redmine #13847 R1-F2: the recovery NEVER bootstraps the fence. A missing / lost fence
        # store must NOT be auto-created here — `DispatchOutboxFence.bootstrap` treats a TOTAL
        # loss (both DB + sidecar gone) as a genuine first-init and mints a fresh store, which
        # would forget an already-`delivered` row and re-send the original request. The redispatch
        # requires an ALREADY-bootstrapped fence and fails closed otherwise (the store-loss
        # contract: missing/corrupt -> zero-send + operator `recover()` + a new action_id).
        return self.fence if self.fence is not None else DispatchOutboxFence()

    def _gateway_live_target(self, gateway_assigned_name: str) -> Tuple[str, str]:
        try:
            rows = self._rows()
        except Exception:  # noqa: BLE001
            return "", ""
        matches = [
            row for row in rows
            if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME)) == _norm(gateway_assigned_name)
        ]
        if len(matches) != 1:
            return "", ""
        revision = matches[0].get("revision")
        if isinstance(revision, bool):
            return "", ""
        return _norm(_agent_locator(matches[0])), _norm(revision)

    def _journal_entries(self, issue: str):
        """Fresh durable authority read; tests may replace this narrow boundary."""

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        return LiveRedmineJournalSource.from_environment(
            environ=self.env
        ).read_entries(_norm(issue))

    def _retry_authority_is_exact(
        self,
        *,
        retry_of_action_id: str,
        issue: str,
        lane: str,
        journal: str,
        approval_journal: str,
        prior_zero_send_journal: str,
        workspace_id: str,
        target_assigned_name: str,
    ) -> bool:
        """Verify action authority from a fresh Redmine read, never CLI self-equality."""

        try:
            entries = tuple(self._journal_entries(issue))
        except (Exception, SystemExit):  # unreadable durable truth fails closed
            return False
        exact_approval = tuple(
            entry
            for entry in entries
            if _norm(getattr(entry, "journal_id", "")) == _norm(approval_journal)
        )
        exact_prior = tuple(
            entry
            for entry in entries
            if _norm(getattr(entry, "journal_id", ""))
            == _norm(prior_zero_send_journal)
        )
        if len(exact_approval) != 1 or len(exact_prior) != 1:
            return False
        authorizations = parse_recovery_delivery_authorizations(exact_approval)
        evidence = parse_recovery_delivery_zero_send_evidence(exact_prior)
        if len(authorizations) != 1 or len(evidence) != 1:
            return False
        return bool(
            authorizations[0].valid_for(
                issue=issue,
                lane=lane,
                workspace_id=workspace_id,
                approval_journal=approval_journal,
                anchor_journal=journal,
                retry_of_action_id=retry_of_action_id,
                prior_zero_send_journal=prior_zero_send_journal,
            )
            and evidence[0].valid_for(
                issue=issue,
                lane=lane,
                workspace_id=workspace_id,
                evidence_journal=prior_zero_send_journal,
                anchor_journal=journal,
                retry_of_action_id=retry_of_action_id,
                target_assigned_name=target_assigned_name,
            )
        )

    def _redispatch_with_action(
        self,
        *,
        action_id: str,
        target_action_id: str,
        gateway_assigned_name: str,
        issue: str,
        lane: str,
        journal: str,
        workspace_id: str,
    ) -> str:
        key = FenceKey(
            workspace_id=_norm(workspace_id), lane_id=_norm_lane(lane), issue=_norm(issue),
            journal=_norm(journal), action_id=_norm(action_id),
            target_assigned_name=_norm(gateway_assigned_name),
        )
        fence = self._fence()
        # Fail closed on a missing / lost / inconsistent fence (Redmine #13847 R1-F2): only an
        # already-bootstrapped, identity-matched fence can prove exactly-once. An un-bootstrapped
        # or lost store is a reconcile condition, never a fresh reserve that could re-send.
        try:
            bootstrapped = fence.is_bootstrapped()
        except Exception:  # noqa: BLE001 - unreadable fence state => uncertain (never send)
            return REDISPATCH_UNCERTAIN
        if not bootstrapped:
            return REDISPATCH_UNCERTAIN
        try:
            reserve = fence.reserve(key)
        except DispatchOutboxFenceError:
            return REDISPATCH_UNCERTAIN
        if not reserve.won:
            # The fence already holds a row for this exact redispatch — idempotent. A
            # delivered/reserved-by-another row is "already"; an uncertain one needs reconcile.
            if reserve.needs_reconcile or reserve.current_state == "uncertain":
                return REDISPATCH_UNCERTAIN
            return REDISPATCH_ALREADY
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
            try:
                fence.mark_cancelled(key, detail=f"target retiring: {why}")
            except DispatchOutboxFenceError:
                return REDISPATCH_UNCERTAIN
            return REDISPATCH_TARGET_RETIRING
        gateway_locator, target_revision = self._gateway_live_target(
            gateway_assigned_name
        )
        decoded = decode_assigned_name(gateway_assigned_name)
        if (
            not gateway_locator
            or not target_revision
            or not decoded.ok
            or decoded.identity is None
        ):
            try:
                fence.mark_cancelled(
                    key, detail="exact live gateway target unresolved; zero-send"
                )
            except DispatchOutboxFenceError:
                return REDISPATCH_UNCERTAIN
            return REDISPATCH_FAILED
        try:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_anchor_delivery_live import (  # noqa: E501
                LiveRecoveryAnchorDeliveryService,
            )

            outcome = LiveRecoveryAnchorDeliveryService(
                repo_root=self.repo_root,
                env=self.env,
                runner=self.runner,
                timeout=self.timeout,
                attestation_home=self.attestation_home,
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
            try:
                fence.mark_uncertain(key, detail="recovery delivery service raised")
            except DispatchOutboxFenceError:
                pass
            return REDISPATCH_UNCERTAIN
        if outcome.started:
            try:
                fence.mark_delivered(
                    key,
                    detail="implementation_request recovery turn-start confirmed",
                )
            except DispatchOutboxFenceError:
                return REDISPATCH_UNCERTAIN
            return REDISPATCH_DELIVERED
        if outcome.zero_send:
            try:
                fence.mark_cancelled(
                    key,
                    detail=f"recovery delivery zero-send: {outcome.detail}",
                )
            except DispatchOutboxFenceError:
                return REDISPATCH_UNCERTAIN
            return REDISPATCH_FAILED
        try:
            fence.mark_uncertain(
                key,
                detail=f"recovery delivery uncertain: {outcome.detail}",
            )
        except DispatchOutboxFenceError:
            pass
        return REDISPATCH_UNCERTAIN

    def redispatch_to_gateway(
        self, *, action_id: str, gateway_assigned_name: str, issue: str, lane: str, journal: str, workspace_id: str
    ) -> str:
        return self._redispatch_with_action(
            action_id=action_id,
            target_action_id=action_id,
            gateway_assigned_name=gateway_assigned_name,
            issue=issue,
            lane=lane,
            journal=journal,
            workspace_id=workspace_id,
        )

    def _retry_delivery_context(
        self,
        *,
        retry_of_action_id: str,
        issue: str,
        lane: str,
        journal: str,
        approval_journal: str,
        prior_zero_send_journal: str,
        workspace_id: str,
    ) -> Tuple[str, str]:
        if not all(
            _norm(value)
            for value in (
                retry_of_action_id,
                issue,
                lane,
                journal,
                approval_journal,
                prior_zero_send_journal,
                workspace_id,
            )
        ):
            return "", "retry_authority_incomplete"
        if _norm(approval_journal) != _norm(self.request_journal):
            return "", "retry_authority_context_mismatch"
        try:
            rec = LaneLifecycleStore(home=self.lifecycle_home).get(
                LaneLifecycleKey(_norm(workspace_id), _norm_lane(lane))
            )
        except (LaneLifecycleError, OSError, ValueError):
            return "", "lifecycle_unreadable"
        if not (
            rec is not None
            and rec.lane_disposition == DISPOSITION_ACTIVE
            and _norm(rec.issue_id) == _norm(issue)
        ):
            return "", "lane_not_active_or_reowned"
        pair = read_declared_pin_pair(rec)
        if not pair.ok or pair.gateway is None:
            return "", "declared_gateway_unresolved"
        provider = _norm(pair.gateway.provider)
        gateway_assigned_name = encode_assigned_name(
            _norm(workspace_id), provider, _norm_lane(lane)
        )
        declared_name = _norm(getattr(pair.gateway, "assigned_name", ""))
        if declared_name and declared_name != gateway_assigned_name:
            return "", "declared_gateway_identity_mismatch"
        if not self._retry_authority_is_exact(
            retry_of_action_id=retry_of_action_id,
            issue=issue,
            lane=lane,
            journal=journal,
            approval_journal=approval_journal,
            prior_zero_send_journal=prior_zero_send_journal,
            workspace_id=workspace_id,
            target_assigned_name=gateway_assigned_name,
        ):
            return "", "retry_authority_unverified"
        prior_key = FenceKey(
            workspace_id=_norm(workspace_id),
            lane_id=_norm_lane(lane),
            issue=_norm(issue),
            journal=_norm(journal),
            action_id=_norm(retry_of_action_id),
            target_assigned_name=gateway_assigned_name,
        )
        try:
            prior_state = self._fence().state_of(prior_key)
        except DispatchOutboxFenceError:
            return "", "prior_fence_unreadable"
        if prior_state == FENCE_DELIVERED:
            return gateway_assigned_name, "prior_delivered"
        if prior_state not in (FENCE_RESERVED, FENCE_UNCERTAIN):
            return "", f"prior_fence_not_reconcilable:{prior_state}"
        locator, revision = self._gateway_live_target(gateway_assigned_name)
        if not locator or not revision:
            return "", "fresh_gateway_unresolved"
        decoded = decode_assigned_name(gateway_assigned_name)
        if not decoded.ok or decoded.identity is None:
            return "", "fresh_gateway_identity_mismatch"
        try:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_anchor_delivery_live import (  # noqa: E501
                LiveRecoveryAnchorDeliveryService,
            )

            delivery_preflight = LiveRecoveryAnchorDeliveryService(
                repo_root=self.repo_root,
                env=self.env,
                runner=self.runner,
                timeout=self.timeout,
                attestation_home=self.attestation_home,
            ).preflight(
                RecoveryAnchorDeliveryRequest(
                    issue=_norm(issue),
                    journal=_norm(journal),
                    kind=KIND_IMPLEMENTATION_REQUEST,
                    workspace_id=_norm(workspace_id),
                    lane_id=_norm_lane(lane),
                    provider=_norm(decoded.identity.role),
                    target_assigned_name=_norm(gateway_assigned_name),
                    target_locator=locator,
                    target_revision=revision,
                    target_action_id=_norm(retry_of_action_id),
                )
            )
        except (Exception, SystemExit):  # unavailable capability is zero-send
            return "", "recovery_delivery_preflight_unreadable"
        if not delivery_preflight.may_deliver:
            return "", f"recovery_delivery_preflight_blocked:{delivery_preflight.detail}"
        return gateway_assigned_name, "ready"

    def preflight_retry_redispatch_to_gateway(
        self,
        *,
        retry_of_action_id: str,
        issue: str,
        lane: str,
        journal: str,
        approval_journal: str,
        prior_zero_send_journal: str,
        workspace_id: str,
    ) -> Tuple[bool, str]:
        gateway, detail = self._retry_delivery_context(
            retry_of_action_id=retry_of_action_id,
            issue=issue,
            lane=lane,
            journal=journal,
            approval_journal=approval_journal,
            prior_zero_send_journal=prior_zero_send_journal,
            workspace_id=workspace_id,
        )
        return bool(gateway), detail

    def retry_redispatch_to_gateway(
        self,
        *,
        action_id: str,
        retry_of_action_id: str,
        issue: str,
        lane: str,
        journal: str,
        approval_journal: str,
        prior_zero_send_journal: str,
        workspace_id: str,
    ) -> str:
        """Use a new key while preserving and proving the exact prior blocked attempt."""
        if not _norm(action_id):
            return REDISPATCH_FAILED
        gateway_assigned_name, detail = self._retry_delivery_context(
            retry_of_action_id=retry_of_action_id,
            issue=issue,
            lane=lane,
            journal=journal,
            approval_journal=approval_journal,
            prior_zero_send_journal=prior_zero_send_journal,
            workspace_id=workspace_id,
        )
        if not gateway_assigned_name:
            return REDISPATCH_FAILED
        if detail == "prior_delivered":
            return REDISPATCH_ALREADY
        return self._redispatch_with_action(
            action_id=action_id,
            target_action_id=retry_of_action_id,
            gateway_assigned_name=gateway_assigned_name,
            issue=issue,
            lane=lane,
            journal=journal,
            workspace_id=workspace_id,
        )


def build_live_recover_pair_use_case(
    *, repo_root: Path, env: Mapping[str, str], issue: str, lane: str, journal: str
) -> SublaneRecoverPairUseCase:
    """Composition root: the live recover-pair use case (real stores + resume + ops).

    The recovery request identity (issue / lane / journal) is bound into the live ops here so
    every quarantine / relaunch / redispatch request it builds carries the exact approved
    anchor — the CLI resolves the request first and passes it in.
    """
    store = LaneLifecycleStore()
    resume = SublaneResumeUseCase(
        ops=LiveSublaneResumeOps(repo_root=repo_root, env=dict(env)), store=store
    )
    ops = LiveHibernatedPairRecoveryOps(
        repo_root=repo_root,
        request_issue=issue,
        request_lane=lane,
        request_journal=journal,
        env=dict(env),
    )
    return SublaneRecoverPairUseCase(ops=ops, store=store, resume=resume)


def build_live_recover_pair_delivery_use_case(
    *, repo_root: Path, env: Mapping[str, str], issue: str, lane: str, journal: str
) -> SublaneRecoverPairDeliveryUseCase:
    """Composition root for the active-pair, new-action recovery delivery."""
    return SublaneRecoverPairDeliveryUseCase(
        ops=LiveHibernatedPairRecoveryOps(
            repo_root=repo_root,
            request_issue=issue,
            request_lane=lane,
            request_journal=journal,
            env=dict(env),
        )
    )


__all__ = (
    "LiveHibernatedPairRecoveryOps",
    "build_live_recover_pair_delivery_use_case",
    "build_live_recover_pair_use_case",
)
