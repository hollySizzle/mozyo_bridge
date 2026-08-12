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
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    DispatchOutboxFenceError,
    FENCE_CANCELLED,
    FENCE_DELIVERED,
    FENCE_RESERVED,
    FENCE_UNCERTAIN,
    FenceKey,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    STORE_ABSENT,
    STORE_RECOGNIZED,
    probe_store_schema,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
    RedispatchEdgeResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.lane_checkout_authority import (  # noqa: E501
    checkout_authority_current,
    current_branch,
    worktree_binding_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    probe_worktree_resolved,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_recover_pair_delivery import (  # noqa: E501
    SublaneRecoverPairDeliveryUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
    REDISPATCH_FAILED,
    REDISPATCH_TARGET_RETIRING,
    REDISPATCH_UNCERTAIN,
    HibernatedPairRecoveryOps,
    SlotPlan,
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
    terminal_identity_of_live_slot,
    derive_lane_workspace_token,
    encode_assigned_name,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_BRANCH_DRIFTED,
    LAUNCH_AUTHORITY_OK,
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
    LAUNCH_AUTHORITY_WORKTREE_UNBOUND,
    LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE,
    LAUNCH_AUTHORITY_WORKTREE_UNREADABLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair

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

    def _checkout_authority_current(self, lane: str) -> bool:
        """Are the lane's checkout axes current RIGHT NOW? (read-only, fail-closed)

        The transport-direct fence (review j#88532 F1), delegated to the shared leaf so the
        preflight axis and the pre-send axis are literally the same code.
        """
        return checkout_authority_current(
            self.repo_root, workspace_id=self.workspace_id(), lane=lane,
            lifecycle_home=self.lifecycle_home,
        )

    def lane_worktree_binding_reason(self, *, lane: str, record) -> str:
        """The lane's canonical worktree-binding axis (Redmine #14475, reviews j#88477 F1 /
        j#88505 F1 / j#88513 F1 / j#88532 F1), delegated to the shared leaf."""
        return worktree_binding_reason(self.repo_root, lane=lane, record=record)

    @staticmethod
    def _current_branch(path: str) -> str:
        """The worktree's current branch, or ``""`` (fail-closed). Read-only."""
        return current_branch(path)

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
            home=self.attestation_home,
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
        try:
            shape = probe_store_schema(herdr_identity_attestation_path(self.attestation_home))
        except Exception:  # noqa: BLE001 - unknown authority preserves every live slot
            return SlotRecoveryObservation(), "", assigned_name
        if not (
            shape.state == STORE_ABSENT
            or (shape.state == STORE_RECOGNIZED
                and shape.version == HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION)
        ):
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
            live_terminal_id=terminal_identity_of_live_slot(
                assigned_name, live_locator, rows
            ),
            expected_workspace_id=workspace_id,
            expected_role=provider,
            expected_lane=lane,
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
                and not join.ok
            ),
            already_healthy=(att_readable and join.ok),
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
        quarantine = self._quarantine()
        request = self._quarantine_request(
            role=role, assigned_name=assigned_name, locator=locator, action_id=action_id
        )
        from .herdr_destructive_close_identity import current_generation_release_pin
        try:
            release = current_generation_release_pin(
                tuple(self._rows()),
                home=self.attestation_home,
                workspace_id=self.workspace_id(),
                lane_id=self.request_lane,
                role=provider,
                assigned_name=assigned_name,
                locator=locator,
            )
        except Exception:  # noqa: BLE001 - destructive authority unreadable
            release = None
        if release is None:
            return False
        try:
            result = quarantine.close_receiver(request, release)
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

    @staticmethod
    def _edge_result(status: str, **facts) -> RedispatchEdgeResult:
        return RedispatchEdgeResult(status=status, **facts)

    def _redispatch_with_action(self, **kwargs) -> RedispatchEdgeResult:
        """Delegate to the extracted edge (module-health leaf; behaviour unchanged)."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_recover_pair_redispatch_edge import (  # noqa: E501
            perform_redispatch,
        )

        return perform_redispatch(self, **kwargs)


    def redispatch_to_gateway(
        self, *, action_id: str, gateway_assigned_name: str, issue: str, lane: str, journal: str, workspace_id: str
    ) -> RedispatchEdgeResult:
        return self._redispatch_with_action(
            action_id=action_id,
            target_action_id=action_id,
            gateway_assigned_name=gateway_assigned_name,
            issue=issue,
            lane=lane,
            journal=journal,
            workspace_id=workspace_id,
            # Review j#88532 F1: the seam existed and neither caller passed it. The send is an
            # owed effect on THIS lane's checkout, so its authority is re-joined at the last
            # external observation before transport — a branch that moved after the relaunch
            # makes this a typed zero-send with the reserve cancelled, never a delivery into a
            # pair standing on the wrong checkout.
            pre_send_authority=lambda: self._checkout_authority_current(lane),
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
    ) -> RedispatchEdgeResult:
        """Use a new key while preserving and proving the exact prior blocked attempt."""
        if not _norm(action_id):
            # Nothing was reserved or sent, and there is nothing owed to reconcile.
            return RedispatchEdgeResult(status=REDISPATCH_FAILED, zero_send=True)
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
            return RedispatchEdgeResult(status=REDISPATCH_FAILED, zero_send=True)
        if detail == "prior_delivered":
            return RedispatchEdgeResult(status=REDISPATCH_ALREADY, zero_send=True)
        return self._redispatch_with_action(
            action_id=action_id,
            target_action_id=retry_of_action_id,
            gateway_assigned_name=gateway_assigned_name,
            issue=issue,
            lane=lane,
            journal=journal,
            workspace_id=workspace_id,
            pre_send_authority=lambda: self._checkout_authority_current(lane),
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
    # ``ops`` first: the resume's commit authority closes over it.
    ops = LiveHibernatedPairRecoveryOps(
        repo_root=repo_root,
        request_issue=issue,
        request_lane=lane,
        request_journal=journal,
        env=dict(env),
    )
    resume = SublaneResumeUseCase(
        ops=LiveSublaneResumeOps(repo_root=repo_root, env=dict(env)), store=store,
        # Redmine #14475 (review j#88538 F1): re-joined INSIDE the resume, immediately before
        # the disposition CAS — the resume's own preflight makes external observations, so a
        # check before ``run()`` is not a check before the active flip.
        commit_authority=lambda: ops._checkout_authority_current(lane),
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
