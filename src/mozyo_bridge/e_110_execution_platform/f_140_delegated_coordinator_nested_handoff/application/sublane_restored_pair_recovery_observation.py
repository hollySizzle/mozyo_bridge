"""Read-only live observation for post-reboot active-pair recovery (#15227)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    ATTEST_ABSENT,
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    probe_worktree_resolved,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery import (  # noqa: E501
    RestoredPairRecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_recovery import (  # noqa: E501
    SLOT_GATEWAY,
    SLOT_WORKER,
    RestoredPairPlan,
    RestoredSlot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_UNKNOWN,
    map_agent_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
    derive_lane_workspace_token,
    encode_assigned_name,
)

_STATUS_KEYS = ("agent_status", "status", "state")


def _git_value(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args], text=True, capture_output=True
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _row_revision(row: Mapping[str, object]) -> str:
    value = row.get("revision")
    return "" if isinstance(value, bool) else _norm(value)


def _row_runtime_state(row: Optional[Mapping[str, object]]) -> str:
    if row is None:
        return RUNTIME_UNKNOWN
    for key in _STATUS_KEYS:
        if key in row:
            return map_agent_status(row.get(key))
    return RUNTIME_UNKNOWN


@dataclass
class LiveRestoredPairObservation:
    repo_root: Path
    env: Mapping[str, str]
    lifecycle_home: Optional[Path] = None
    attestation_home: Optional[Path] = None

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return list_herdr_agent_rows(self.env)

    def _lifecycle(self, workspace_id: str, lane: str):
        try:
            return LaneLifecycleStore(home=self.lifecycle_home).get(
                LaneLifecycleKey(workspace_id, lane)
            )
        except Exception:  # noqa: BLE001 - unreadable authority fails closed in the plan
            return None

    def _slot(
        self,
        *,
        slot_role: str,
        provider: str,
        workspace_id: str,
        lane: str,
        rows: Sequence[Mapping[str, object]],
        supplied_name: str,
        supplied_locator: str,
        supplied_revision: str,
    ) -> RestoredSlot:
        try:
            expected_name = encode_assigned_name(workspace_id, provider, lane)
        except Exception:  # noqa: BLE001 - incomplete identity remains an incomplete plan
            expected_name = ""
        assigned_name = _norm(supplied_name) or expected_name
        named = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == assigned_name
        ]
        row = named[0] if len(named) == 1 else None
        observed_locator = _agent_locator(row) if row is not None else ""
        observed_revision = _row_revision(row) if row is not None else ""
        locator = _norm(supplied_locator) or observed_locator
        revision = _norm(supplied_revision) or observed_revision
        inventory_generation_matches = bool(
            row is not None
            and observed_locator
            and observed_revision
            and observed_locator == locator
            and observed_revision == revision
        )

        decoded = decode_assigned_name(assigned_name)
        identity_matches = bool(
            assigned_name == expected_name
            and decoded.ok
            and decoded.identity is not None
            and decoded.identity.workspace_id == workspace_id
            and decoded.identity.role == provider
            and _norm_lane(decoded.identity.lane_id) == lane
        )
        cwd_matches = False
        if row is not None:
            cwd = _norm(row.get("foreground_cwd") or row.get("cwd"))
            if cwd:
                try:
                    cwd_matches = Path(cwd).expanduser().resolve() == self.repo_root.resolve()
                except OSError:
                    cwd_matches = False

        attestation_state = ATTEST_ABSENT
        attestation_readable = True
        try:
            attestation = HerdrIdentityAttestationStore(home=self.attestation_home).read(
                assigned_name
            )
            attestation_state = evaluate_attestation(
                attestation,
                live_locator=observed_locator,
                expected_workspace_id=workspace_id,
                expected_role=provider,
                expected_lane=lane,
            ).state
        except Exception:  # noqa: BLE001 - unreadable store is not bad-generation proof
            attestation_readable = False

        return RestoredSlot(
            slot_role=slot_role,
            provider=provider,
            assigned_name=assigned_name,
            locator=locator,
            revision=revision,
            identity_matches=identity_matches,
            inventory_generation_matches=inventory_generation_matches,
            runtime_state=_row_runtime_state(row),
            cwd_matches=cwd_matches,
            attestation_state=attestation_state,
            attestation_readable=attestation_readable,
        )

    def observe(self, request: RestoredPairRecoveryRequest) -> RestoredPairPlan:
        issue = _norm(request.issue)
        lane = _norm_lane(request.lane)
        repo_root = self.repo_root.expanduser().resolve()
        try:
            workspace_id = _norm(repo_scope_workspace_id(repo_root))
            rows = self._rows()
            gateway_provider = _norm(resolve_gateway_provider(str(repo_root)))
            worker_provider = _norm(resolve_worker_provider(str(repo_root)))
        except Exception:  # noqa: BLE001 - incomplete plan is the fail-closed result
            workspace_id, rows, gateway_provider, worker_provider = "", (), "", ""

        record = self._lifecycle(workspace_id, lane) if workspace_id and lane else None
        lane_revision = str(getattr(record, "revision", "") or "")
        lane_generation = str(getattr(record, "lane_generation", "") or "")
        lifecycle_current = bool(
            record is not None
            and record.lane_disposition == DISPOSITION_ACTIVE
            and _norm(record.issue_id) == issue
            and _norm_lane(record.lane_id) == lane
            and lane_revision.isdecimal()
            and lane_generation.isdecimal()
            and int(lane_revision) > 0
            and int(lane_generation) > 0
        )

        branch = _git_value(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        head = _git_value(repo_root, "rev-parse", "HEAD")
        worktree_identity = _norm(getattr(record, "worktree_identity", ""))
        worktree_reason = "ok"
        try:
            derived = _norm(derive_lane_workspace_token(str(repo_root)))
        except Exception:  # noqa: BLE001
            derived = ""
        if not worktree_identity:
            worktree_reason = "worktree_unbound"
        elif not derived or derived != worktree_identity:
            worktree_reason = "worktree_identity_mismatch"
        else:
            try:
                readable = probe_worktree_resolved(str(repo_root)) is True
            except Exception:  # noqa: BLE001 - unreadable worktree authority fails closed
                readable = False
            if not readable:
                worktree_reason = "worktree_unreadable"
        if worktree_reason == "ok" and _norm_lane(branch) != lane:
            worktree_reason = "branch_drifted"
        elif worktree_reason == "ok" and not head:
            worktree_reason = "head_unreadable"

        gateway = self._slot(
            slot_role=SLOT_GATEWAY,
            provider=gateway_provider,
            workspace_id=workspace_id,
            lane=lane,
            rows=rows,
            supplied_name=request.gateway_assigned_name,
            supplied_locator=request.gateway_locator,
            supplied_revision=request.gateway_revision,
        )
        worker = self._slot(
            slot_role=SLOT_WORKER,
            provider=worker_provider,
            workspace_id=workspace_id,
            lane=lane,
            rows=rows,
            supplied_name=request.worker_assigned_name,
            supplied_locator=request.worker_locator,
            supplied_revision=request.worker_revision,
        )
        return RestoredPairPlan(
            issue=issue,
            lane=lane,
            workspace_id=workspace_id,
            worktree_identity=worktree_identity,
            branch=branch,
            head=head,
            lane_revision=lane_revision,
            lane_generation=lane_generation,
            lifecycle_current=lifecycle_current,
            worktree_authority_current=worktree_reason == "ok",
            worktree_authority_reason=worktree_reason,
            allow_pending_composer_loss=request.allow_pending_composer_loss,
            gateway=gateway,
            worker=worker,
        )


__all__ = ("LiveRestoredPairObservation",)
