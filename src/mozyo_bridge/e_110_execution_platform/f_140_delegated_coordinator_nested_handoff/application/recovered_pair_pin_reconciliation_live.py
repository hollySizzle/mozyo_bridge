"""Live authority join for bounded recovered active-pair pin reconciliation (#14203 R19)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    BINDING_BOUND,
    HerdrIdentityReplacementBindingStore,
    selected_attestation_store_is_v1,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
)
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.core.state.lane_recovered_pair_pin_reconcile import (
    LaneRecoveredPairPinReconcileStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
    LiveRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovered_pair_pin_reconciliation import (  # noqa: E501
    RecoveredPairPinReconciliationUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_anchor_delivery_live import (  # noqa: E501
    LiveRecoveryAnchorDeliveryService,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
    declared_lane_root_identity,
    resolve_declared_pins,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E501
    hibernated_pair_recovery_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_pair_pin_reconciliation import (  # noqa: E501
    RecoveredPairPinReconciliationPreflight,
    RecoveredPairPinReconciliationRequest,
    is_exact_reconciliation_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    KIND_IMPLEMENTATION_REQUEST,
    RecoveryAnchorDeliveryRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


@dataclass(frozen=True)
class _ReconciliationContext:
    preflight: RecoveredPairPinReconciliationPreflight
    workspace_id: str = ""
    worktree_identity: str = ""
    old_slots: tuple[ProcessGenerationPin, ...] = ()
    recovered_slots: tuple[ProcessGenerationPin, ...] = ()


@dataclass
class LiveRecoveredPairPinReconciliationOps:
    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    lifecycle_home: Path | None = None
    attestation_home: Path | None = None

    def _entries(self, issue: str):
        return LiveRedmineJournalSource.from_environment(
            environ=self.env
        ).read_entries(_norm(issue))

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return tuple(list_herdr_agent_rows(self.env))

    def _target_ready(
        self, root: Path, request: RecoveryAnchorDeliveryRequest
    ):
        return LiveRecoveryAnchorDeliveryService(
            repo_root=root,
            env=self.env,
            attestation_home=self.attestation_home,
        ).preflight(request)

    def _attestation_is_v1(self, home: Path) -> bool:
        return selected_attestation_store_is_v1(home)

    def _read_attestation(self, assigned_name: str):
        return HerdrIdentityAttestationStore(
            home=self.attestation_home
        ).read(assigned_name)

    @staticmethod
    def _providers(root: Path) -> tuple[str, str]:
        return (
            resolve_gateway_provider(str(root)),
            resolve_worker_provider(str(root)),
        )

    def _replacement_binding(
        self, home: Path, action_id: str, assigned_name: str
    ):
        return HerdrIdentityReplacementBindingStore(home=home).read(
            action_id, assigned_name
        )

    def _worktree(
        self, request: RecoveredPairPinReconciliationRequest
    ) -> tuple[Path | None, str, str]:
        try:
            root = Path(request.worktree).expanduser().resolve(strict=True)
            workspace = herdr_workspace_segment(root)
            # #14715: one canonical derivation for every surface — the family is a fact
            # about this root's kind, never about the caller's cwd.
            identity = declared_lane_root_identity(
                root, _norm(request.lane)
            ).metadata_token
        except (OSError, ValueError):
            return None, "", ""
        return root, _norm(workspace), _norm(identity)

    @staticmethod
    def _slot_rows(
        rows: Sequence[Mapping[str, object]],
        slots: tuple[ProcessGenerationPin, ...],
    ) -> dict[str, Mapping[str, object]] | None:
        found: dict[str, Mapping[str, object]] = {}
        for slot in slots:
            matches = [
                row
                for row in rows
                if _norm(row.get(AGENT_KEY_NAME)) == _norm(slot.assigned_name)
            ]
            if len(matches) != 1:
                return None
            row = matches[0]
            if _norm(_agent_locator(row)) != _norm(slot.locator):
                return None
            found[slot.assigned_name] = row
        return found

    def _context(
        self, request: RecoveredPairPinReconciliationRequest
    ) -> _ReconciliationContext:
        blocked = lambda detail: _ReconciliationContext(  # noqa: E731
            RecoveredPairPinReconciliationPreflight(False, detail)
        )
        root, workspace, worktree_identity = self._worktree(request)
        if root is None or not workspace or not worktree_identity:
            return blocked("worktree_identity_unresolved")
        try:
            expected_action = hibernated_pair_recovery_action_id(
                issue=_norm(request.issue),
                lane_id=_norm(request.lane),
                revision=str(request.source_revision),
                generation=str(request.lane_generation),
            )
        except ValueError:
            return blocked("recovery_action_unresolved")
        if expected_action != _norm(request.target_action_id):
            return blocked("recovery_action_mismatch")

        try:
            entries = tuple(self._entries(request.issue))
        except (Exception, SystemExit):
            return blocked("owner_authority_unreadable")
        exact_entries = tuple(
            entry
            for entry in entries
            if _norm(getattr(entry, "journal_id", "")) == _norm(request.journal)
        )
        if len(exact_entries) != 1 or not is_exact_reconciliation_authority(
            exact_entries[0], request
        ):
            return blocked("owner_authority_unverified")

        lifecycle = LaneLifecycleStore(home=self.lifecycle_home)
        key = LaneLifecycleKey(workspace, _norm(request.lane))
        try:
            record = lifecycle.get(key)
        except (Exception, SystemExit):
            return blocked("lifecycle_unreadable")
        if not (
            record is not None
            and record.lane_disposition == DISPOSITION_ACTIVE
            and _norm(record.issue_id) == _norm(request.issue)
            and record.revision
            in (request.expected_revision, request.expected_revision + 1)
            and record.lane_generation == request.lane_generation
            and _norm(record.worktree_identity) == worktree_identity
            and _norm(record.decision_source) == "redmine"
            and _norm(record.decision_issue_id) == _norm(request.issue)
            and _norm(record.decision_journal)
            == _norm(request.lifecycle_decision_journal)
        ):
            return blocked("active_lifecycle_authority_moved")

        pair = read_declared_pin_pair(record)
        if not pair.ok or pair.gateway is None or pair.worker is None:
            return blocked("old_declared_pair_unresolved")
        old_slots = (pair.gateway, pair.worker)
        try:
            rows = tuple(
                row for row in self._rows() if isinstance(row, Mapping)
            )
            providers = self._providers(root)
            recovered, reason = resolve_declared_pins(
                rows,
                workspace_id=workspace,
                lane_id=_norm(request.lane),
                providers=providers,
                attestation_store=SimpleNamespace(
                    read=self._read_attestation
                ),
            )
        except (Exception, SystemExit):
            return blocked("recovered_pair_unreadable")
        if recovered is None or len(recovered) != 2:
            return blocked(reason or "recovered_pair_unresolved")
        recovered_slots = tuple(recovered)
        if tuple(
            (slot.provider, slot.assigned_name) for slot in old_slots
        ) != tuple(
            (slot.provider, slot.assigned_name) for slot in recovered_slots
        ):
            return blocked("recovered_pair_identity_moved")
        same_snapshot = tuple(slot.locator for slot in old_slots) == tuple(
            slot.locator for slot in recovered_slots
        )
        replay = (
            same_snapshot
            and record.revision == request.expected_revision + 1
        )
        if same_snapshot and not replay:
            return blocked("recovered_pair_not_newer")
        if not same_snapshot and record.revision != request.expected_revision:
            return blocked("active_lifecycle_authority_moved")

        slot_rows = self._slot_rows(rows, recovered_slots)
        if slot_rows is None:
            return blocked("recovered_pair_live_identity_moved")
        home = self.attestation_home or mozyo_bridge_home()
        try:
            v1 = self._attestation_is_v1(home)
        except Exception:
            return blocked("attestation_generation_unresolved")
        for old, fresh in zip(old_slots, recovered_slots):
            row = slot_rows[fresh.assigned_name]
            revision = row.get("revision")
            if isinstance(revision, bool) or not _norm(revision):
                return blocked("recovered_pair_revision_unresolved")
            ready = self._target_ready(
                root,
                RecoveryAnchorDeliveryRequest(
                    issue=_norm(request.issue),
                    journal=_norm(request.journal),
                    kind=KIND_IMPLEMENTATION_REQUEST,
                    workspace_id=workspace,
                    lane_id=_norm(request.lane),
                    provider=_norm(fresh.provider),
                    target_assigned_name=_norm(fresh.assigned_name),
                    target_locator=_norm(fresh.locator),
                    target_revision=_norm(revision),
                    target_action_id=_norm(request.target_action_id),
                ),
            )
            if not ready.may_deliver:
                return blocked(f"recovered_pair_not_settled:{ready.detail}")
            if v1:
                try:
                    binding = self._replacement_binding(
                        home,
                        _norm(request.target_action_id),
                        _norm(fresh.assigned_name),
                    )
                except Exception:
                    return blocked("replacement_binding_unreadable")
                if not (
                    binding is not None
                    and binding.phase == BINDING_BOUND
                    and _norm(binding.locator) == _norm(fresh.locator)
                    and (
                        (
                            replay
                            and _norm(binding.old_locator)
                            != _norm(fresh.locator)
                        )
                        or (
                            not replay
                            and _norm(binding.old_locator)
                            == _norm(old.locator)
                        )
                    )
                ):
                    return blocked("replacement_binding_old_generation_mismatch")

        return _ReconciliationContext(
            RecoveredPairPinReconciliationPreflight(
                True,
                "ready",
                workspace_id=workspace,
                old_locators=tuple(slot.locator for slot in old_slots),
                recovered_locators=tuple(
                    slot.locator for slot in recovered_slots
                ),
            ),
            workspace_id=workspace,
            worktree_identity=worktree_identity,
            old_slots=old_slots,
            recovered_slots=recovered_slots,
        )

    def preflight(
        self, request: RecoveredPairPinReconciliationRequest
    ) -> RecoveredPairPinReconciliationPreflight:
        return self._context(request).preflight

    def reconcile(
        self, request: RecoveredPairPinReconciliationRequest
    ) -> tuple[bool, int | None, str]:
        context = self._context(request)
        if not context.preflight.ready:
            return False, None, context.preflight.detail
        store = LaneRecoveredPairPinReconcileStore(home=self.lifecycle_home)
        try:
            result = store.reconcile(
                LaneLifecycleKey(
                    context.workspace_id, _norm(request.lane)
                ),
                expected_revision=request.expected_revision,
                expected_generation=request.lane_generation,
                issue_id=_norm(request.issue),
                worktree_identity=context.worktree_identity,
                lifecycle_decision=DecisionPointer(
                    source="redmine",
                    issue_id=_norm(request.issue),
                    journal_id=_norm(request.lifecycle_decision_journal),
                ),
                expected_old_slots=context.old_slots,
                recovered_slots=context.recovered_slots,
            )
        except (Exception, SystemExit) as exc:
            return False, None, type(exc).__name__
        return result.applied, result.revision, result.reason


def build_live_recovered_pair_pin_reconciliation(
    repo_root: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> RecoveredPairPinReconciliationUseCase:
    return RecoveredPairPinReconciliationUseCase(
        ops=LiveRecoveredPairPinReconciliationOps(
            repo_root=Path(repo_root),
            env=dict(env or os.environ),
        )
    )


__all__ = (
    "LiveRecoveredPairPinReconciliationOps",
    "build_live_recovered_pair_pin_reconciliation",
)
