"""Receipt-bound workspace teardown for an owned disposable Herdr server (#15227).

This sibling owns the destructive workspace-cleanup authority only.  Generic Herdr
client calls and graceful owned-server stop remain in ``disposable_herdr_instance``.
"""

from __future__ import annotations

import os
import subprocess
import weakref
from typing import Callable, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_observation import (  # noqa: E501
    SharedSpaceSmokeError,
    SuccessfulWorkspaceCreateReceipts,
    _is_minted_workspace_create_receipts,
)


# Closed refusal vocabulary. Raw workspace ids never enter evidence or exceptions.
REFUSAL_CAPABILITY_ABSENT = "capability_absent"
REFUSAL_CAPABILITY_NOT_MINTED = "capability_not_minted"
REFUSAL_ENDPOINT_UNBOUND = "endpoint_unbound"
REFUSAL_ENDPOINT_OUTSIDE_OWNED_ROOT = "endpoint_outside_owned_root"
REFUSAL_OPERATOR_ENDPOINT_TARGET = "operator_endpoint_target"
REFUSAL_OWNED_CHILD_NOT_ALIVE = "owned_child_not_alive"
REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER = "cleanup_authority_not_owner"
REFUSAL_COMMAND_NOT_ALLOWLISTED = "command_not_allowlisted"
REFUSAL_WORKSPACE_CLEANUP_NOT_MINTED = "workspace_cleanup_not_minted"
REFUSAL_WORKERS_NOT_CONTAINED = "workers_not_contained"
REFUSAL_WORKSPACE_RECEIPT_ABSENT = "workspace_receipt_absent"
REFUSAL_WORKSPACE_RECEIPT_INVALID = "workspace_receipt_invalid"
REFUSAL_WORKSPACE_NOT_RECEIPTED = "workspace_not_receipted"
REFUSAL_WORKSPACE_CLEANUP_CONSUMED = "workspace_cleanup_consumed"
REFUSAL_WORKSPACE_CLOSE_FAILED = "workspace_close_failed"


class SmokeEndpointEscapeError(SharedSpaceSmokeError):
    """A Herdr request was refused before dispatch because ownership was unproven."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        message = f"refused unbound herdr request ({reason})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


_WORKSPACE_CLEANUP_CAPABILITY_TOKEN = object()
_MINTED_WORKSPACE_CLEANUP_CAPABILITIES: (
    "weakref.WeakSet[OwnedWorkspaceCleanupCapability]"
) = weakref.WeakSet()


class OwnedWorkspaceCleanupCapability:
    """One-shot authority with a private, immutable successful-receipt target set."""

    def __init__(
        self,
        controller: "OwnedWorkspaceCleanupController",
        endpoint_capability: object,
        workspace_ids: tuple[str, ...],
        *,
        _mint_token: object = None,
    ) -> None:
        if _mint_token is not _WORKSPACE_CLEANUP_CAPABILITY_TOKEN:
            raise SmokeEndpointEscapeError(REFUSAL_WORKSPACE_CLEANUP_NOT_MINTED)
        self._controller = controller
        self._endpoint_capability = endpoint_capability
        self._workspace_ids = workspace_ids
        self._minter_pid = os.getpid()
        self._consumed = False

    def __repr__(self) -> str:
        return (
            "<OwnedWorkspaceCleanupCapability "
            f"workspace_count={len(self._workspace_ids)} consumed={self._consumed}>"
        )

    def close_all(self) -> bool:
        """Consume this authority and close its exact receipt-bound target set."""
        return self._controller.consume(self)


def _mint_cleanup_capability(
    controller: "OwnedWorkspaceCleanupController",
    endpoint_capability: object,
    workspace_ids: tuple[str, ...],
) -> OwnedWorkspaceCleanupCapability:
    capability = OwnedWorkspaceCleanupCapability(
        controller,
        endpoint_capability,
        workspace_ids,
        _mint_token=_WORKSPACE_CLEANUP_CAPABILITY_TOKEN,
    )
    _MINTED_WORKSPACE_CLEANUP_CAPABILITIES.add(capability)
    return capability


class OwnedWorkspaceCleanupController:
    """Minter-only lifecycle state and dedicated ``workspace close`` dispatcher."""

    def __init__(
        self,
        *,
        endpoint_provider: Callable[[], Optional[object]],
        is_minting_process: Callable[[object], bool],
        process_provider: Callable[[], Optional[object]],
        runner_provider: Callable[[], object],
        binary: str,
        timeout: float,
    ) -> None:
        self._endpoint_provider = endpoint_provider
        self._is_minting_process = is_minting_process
        self._process_provider = process_provider
        self._runner_provider = runner_provider
        self._binary = binary
        self._timeout = timeout
        self._capability: Optional[OwnedWorkspaceCleanupCapability] = None
        self._active: Optional[tuple[object, str]] = None
        self._worker_fleet_entered = False
        self._workers_contained = False
        self.capability_minted = False
        self.attempted = False
        self.completed = False
        self.close_dispatched = 0
        self.refusal = ""

    def withhold_workers(self) -> None:
        self._worker_fleet_entered = True
        self._workers_contained = False

    def contain_workers(self) -> None:
        self._workers_contained = self._worker_fleet_entered

    def refusal_reason(self, cleanup_capability: object, workspace_id: str) -> str:
        """Gate one dedicated close at the final pre-dispatch boundary."""
        endpoint_capability = self._endpoint_provider()
        if endpoint_capability is None:
            return REFUSAL_CAPABILITY_ABSENT
        if (
            not isinstance(cleanup_capability, OwnedWorkspaceCleanupCapability)
            or cleanup_capability not in _MINTED_WORKSPACE_CLEANUP_CAPABILITIES
            or cleanup_capability is not self._capability
            or cleanup_capability._endpoint_capability is not endpoint_capability
        ):
            return REFUSAL_WORKSPACE_CLEANUP_NOT_MINTED
        if os.getpid() != cleanup_capability._minter_pid or not self._is_minting_process(
            endpoint_capability
        ):
            return REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER
        if not self._worker_fleet_entered or not self._workers_contained:
            return REFUSAL_WORKERS_NOT_CONTAINED
        if self._active != (cleanup_capability, workspace_id):
            return REFUSAL_WORKSPACE_CLEANUP_NOT_MINTED
        if workspace_id not in cleanup_capability._workspace_ids:
            return REFUSAL_WORKSPACE_NOT_RECEIPTED
        process = self._process_provider()
        if process is None or process.poll() is not None:
            return REFUSAL_OWNED_CHILD_NOT_ALIVE
        return ""

    def mint(
        self, receipts: SuccessfulWorkspaceCreateReceipts
    ) -> Optional[OwnedWorkspaceCleanupCapability]:
        """Mint only from a recorder-owned successful-create receipt set."""
        self.refusal = ""
        endpoint_capability = self._endpoint_provider()
        if endpoint_capability is None:
            self.refusal = REFUSAL_CAPABILITY_ABSENT
            return None
        if not self._is_minting_process(endpoint_capability):
            self.refusal = REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER
            return None
        process = self._process_provider()
        if process is None or process.poll() is not None:
            self.refusal = REFUSAL_OWNED_CHILD_NOT_ALIVE
            return None
        if not self._worker_fleet_entered or not self._workers_contained:
            self.refusal = REFUSAL_WORKERS_NOT_CONTAINED
            return None
        if self._capability is not None:
            self.refusal = REFUSAL_WORKSPACE_CLEANUP_CONSUMED
            return None
        if not _is_minted_workspace_create_receipts(receipts):
            self.refusal = REFUSAL_WORKSPACE_RECEIPT_INVALID
            return None
        supplied = receipts._workspace_ids
        if not supplied:
            self.refusal = REFUSAL_WORKSPACE_RECEIPT_ABSENT
            return None
        if any(
            not isinstance(workspace_id, str)
            or not workspace_id
            or workspace_id != workspace_id.strip()
            for workspace_id in supplied
        ):
            self.refusal = REFUSAL_WORKSPACE_RECEIPT_INVALID
            return None
        exact_ids = tuple(dict.fromkeys(supplied))
        cleanup = _mint_cleanup_capability(self, endpoint_capability, exact_ids)
        self._capability = cleanup
        self.capability_minted = True
        return cleanup

    def consume(self, cleanup: OwnedWorkspaceCleanupCapability) -> bool:
        """Consume once; never replay an uncertain target generation."""
        if (
            not isinstance(cleanup, OwnedWorkspaceCleanupCapability)
            or cleanup not in _MINTED_WORKSPACE_CLEANUP_CAPABILITIES
            or cleanup is not self._capability
        ):
            self.refusal = REFUSAL_WORKSPACE_CLEANUP_NOT_MINTED
            return False
        if cleanup._consumed:
            self.refusal = REFUSAL_WORKSPACE_CLEANUP_CONSUMED
            return False
        cleanup._consumed = True
        self.attempted = True
        completed = True
        runner = self._runner_provider()
        for workspace_id in cleanup._workspace_ids:
            self._active = (cleanup, workspace_id)
            dispatched_before = getattr(runner, "dispatched_calls", 0)
            try:
                result = runner._close_owned_workspace(
                    self._binary,
                    cleanup,
                    workspace_id,
                    timeout=self._timeout,
                )
            except SmokeEndpointEscapeError as exc:
                self.refusal = exc.reason
                completed = False
                break
            except (OSError, subprocess.TimeoutExpired):
                self.refusal = REFUSAL_WORKSPACE_CLOSE_FAILED
                completed = False
                break
            finally:
                self.close_dispatched += int(
                    getattr(runner, "dispatched_calls", 0) > dispatched_before
                )
                self._active = None
            if getattr(result, "returncode", 1) != 0:
                self.refusal = REFUSAL_WORKSPACE_CLOSE_FAILED
                completed = False
        self.completed = bool(
            completed and self.close_dispatched == len(cleanup._workspace_ids)
        )
        return self.completed

    def invalidate(self) -> None:
        self._capability = None
        self._active = None

    def as_evidence(self) -> dict[str, object]:
        """Counts, booleans and a closed reason only; never a runtime identity."""
        return {
            "workspace_cleanup_capability_minted": self.capability_minted,
            "workspace_cleanup_attempted": self.attempted,
            "workspace_cleanup_completed": self.completed,
            "workspace_close_dispatched": self.close_dispatched,
            "workspace_cleanup_refusal": self.refusal,
        }


__all__ = (
    "OwnedWorkspaceCleanupCapability",
    "OwnedWorkspaceCleanupController",
    "SmokeEndpointEscapeError",
    "REFUSAL_CAPABILITY_ABSENT",
    "REFUSAL_CAPABILITY_NOT_MINTED",
    "REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER",
    "REFUSAL_COMMAND_NOT_ALLOWLISTED",
    "REFUSAL_ENDPOINT_OUTSIDE_OWNED_ROOT",
    "REFUSAL_ENDPOINT_UNBOUND",
    "REFUSAL_OPERATOR_ENDPOINT_TARGET",
    "REFUSAL_OWNED_CHILD_NOT_ALIVE",
    "REFUSAL_WORKERS_NOT_CONTAINED",
    "REFUSAL_WORKSPACE_CLEANUP_CONSUMED",
    "REFUSAL_WORKSPACE_CLEANUP_NOT_MINTED",
    "REFUSAL_WORKSPACE_CLOSE_FAILED",
    "REFUSAL_WORKSPACE_NOT_RECEIPTED",
    "REFUSAL_WORKSPACE_RECEIPT_ABSENT",
    "REFUSAL_WORKSPACE_RECEIPT_INVALID",
)
