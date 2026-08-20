"""The shared authority seams both restored-pair rails observe and write through (#15811).

Extracted verbatim from :class:`...sublane_restored_pair_rebind_live
.LiveRestoredPairRebindOps` (#15656 / #15769) when the pin-ABSENT adopt rail
(:mod:`.sublane_restored_pair_adopt_live`) needed the SAME set: the two rails differ only
in what they may conclude from the evidence, never in where the evidence comes from. Both
inherit this base, so a fake that overrides a host probe in one rail's regression overrides
the identical probe in the other's, and a store binding cannot drift between them.

Every method is an overridable seam so a regression can fake the HOST probes (repo root,
workspace id, worktree token / readability, branch, providers, inventory) while the STORE
joins below stay real against a temp home. Read seams raise on a damaged authority — an
unreadable store is never folded into "absent" by this layer; the caller's gate decides
what a raise means.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
)
from mozyo_bridge.core.state.herdr_launch_generation import HerdrLaunchGenerationStore
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleKey, LaneLifecycleStore
from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
    declared_worktree_identity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    probe_worktree_resolved,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)


@dataclass
class RestoredPairStoreSeams:
    """Host probes + durable-store joins for the restored-pair rails (test seams)."""

    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    lifecycle_home: Optional[Path] = None
    attestation_home: Optional[Path] = None

    # -- host probes ----------------------------------------------------------

    def _resolve_root(self) -> Optional[Path]:
        try:
            root = self.repo_root.expanduser().resolve(strict=True)
        except OSError:
            return None
        return root if root.is_dir() else None

    def _workspace_id(self, root: Path) -> str:
        return _norm(repo_scope_workspace_id(root))

    def _worktree_identity(self, root: Path, lane: str) -> Optional[str]:
        return declared_worktree_identity(str(root), lane)

    def _worktree_readable(self, root: Path) -> bool:
        try:
            return probe_worktree_resolved(str(root)) is True
        except Exception:  # noqa: BLE001 - unreadable worktree authority fails closed
            return False

    def _branch(self, root: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
                capture_output=True,
            )
        except OSError:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def _providers(self, root: Path) -> tuple[str, str]:
        return (
            _norm(resolve_gateway_provider(str(root))),
            _norm(resolve_worker_provider(str(root))),
        )

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return list_herdr_agent_rows(self.env)

    # -- durable-store reads --------------------------------------------------

    def _lifecycle_record(self, workspace_id: str, lane: str):
        return LaneLifecycleStore(home=self.lifecycle_home).get(
            LaneLifecycleKey(workspace_id, lane)
        )

    def _read_attestation(self, assigned_name: str):
        return HerdrIdentityAttestationStore(home=self.attestation_home).read(
            assigned_name
        )

    def _read_generation(self, assigned_name: str):
        """The current launch-generation row for this slot (raises when unreadable)."""
        return HerdrLaunchGenerationStore(home=self.attestation_home).read(assigned_name)

    def _read_startup_action(self, action_id: str):
        """The startup-transaction action a generation token names (raises on damage)."""
        return StartupTransactionFence(home=self.attestation_home).read(action_id)

    # -- durable-store re-attest writes (each its own byte-exact CAS) ----------

    def _repin_participant(self, plan) -> None:
        repair = plan.participant
        assert repair is not None and repair.needs_write
        kwargs = {
            "assigned_name": plan.assigned_name,
            "expected_locator": repair.expected_locator,
        }
        if repair.locator_repin:
            kwargs["new_locator"] = plan.new_locator
        if repair.receipt_remint:
            kwargs["expected_receipt"] = repair.expected_receipt
            kwargs["new_receipt"] = repair.new_receipt
        StartupTransactionFence(home=self.attestation_home).repin_restored_participant(
            plan.startup_action_id, plan.provider, **kwargs
        )

    def _reattest_attestation(self, plan) -> None:
        HerdrIdentityAttestationStore(
            home=self.attestation_home
        ).reattest_restored_terminal(
            assigned_name=plan.assigned_name,
            workspace_id=plan.attestation_workspace_id,
            role=plan.attestation_role,
            lane_id=plan.attestation_lane_id,
            expected_locator=plan.attestation_expected_locator,
            expected_terminal_id=plan.attestation_expected_terminal_id,
            live_locator=plan.new_locator,
            live_terminal_id=plan.new_terminal_id,
        )

    def _reattest_generation(self, plan) -> None:
        HerdrLaunchGenerationStore(
            home=self.attestation_home
        ).reattest_restored_terminal(
            assigned_name=plan.assigned_name,
            startup_action_id=plan.startup_action_id,
            workspace_id=plan.workspace_id,
            role=plan.provider,
            lane_id=plan.lane_id,
            verdict=plan.verdict,
            expected_locator=plan.old_locator,
            expected_terminal_id=plan.old_terminal_id,
            live_locator=plan.new_locator,
            live_terminal_id=plan.new_terminal_id,
        )


__all__ = ("RestoredPairStoreSeams",)
