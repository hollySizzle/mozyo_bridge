"""Canonical send edge for a recovered vanished gateway (#14741 B6b3-2b).

The stored continuation, fresh inventory generation, startup attestation, and replacement
action are re-joined once as an authority check and once again immediately before the
governed send.  Only ``HerdrSublaneActuatorOps.dispatch_implementation_request`` may issue
the effect.  A zero return code means that one send was attempted; it never means that the
gate landed or that the replacement transaction completed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
    DRAIN_SEND_ZERO,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_continuation import (  # noqa: E501
    CONTINUATION_READY,
    ContinuationPreparation,
    VanishedGatewayAttestationEvidence,
    VanishedGatewayAttestationProof,
    VanishedGatewayInventoryJoin,
    resolve_vanished_gateway_inventory,
    verify_vanished_gateway_attestation_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E501
    REDISPATCH_GATEWAY_ONCE,
    RESUME_GATE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

SEND_ATTEMPTED = "send_attempted"
SEND_AUTHORITY_INVALID = "send_authority_invalid"
SEND_AUTHORITY_MOVED = "send_authority_moved"
SEND_FAILED = "send_failed"


def _plain(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        return ""
    return value


def _canonical_root(value: object) -> Optional[Path]:
    concrete_path_type = type(Path())
    if type(value) is str:
        raw = _plain(value)
        if not raw:
            return None
        candidate = Path(raw)
    elif type(value) is concrete_path_type:
        candidate = value
    else:
        return None
    try:
        if not candidate.is_absolute():
            return None
        resolved = candidate.resolve(strict=True)
        if candidate != resolved or not resolved.is_dir():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


@dataclass(frozen=True)
class VanishedGatewaySendAuthority:
    """All exact action-time axes consumed by one canonical send attempt."""

    action_id: str
    workspace_id: str
    lane_id: str
    provider: str
    assigned_name: str
    fresh_locator: str
    old_locator: str
    observed_at: str
    startup_action_id: str
    revision: int


@dataclass(frozen=True)
class VanishedGatewaySendResult:
    """A typed send-attempt result; never a delivery or completion verdict."""

    status: str
    detail: str
    action_id: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    provider: str = ""
    assigned_name: str = ""
    fresh_locator: str = ""
    old_locator: str = ""
    observed_at: str = ""

    @property
    def attempted(self) -> bool:
        return self.status == DRAIN_SEND_OK

    @property
    def zero_send(self) -> bool:
        return self.status == DRAIN_SEND_ZERO


def _result(status: str, detail: str) -> VanishedGatewaySendResult:
    return VanishedGatewaySendResult(status=status, detail=detail)


@dataclass
class VanishedGatewayContinuationOps:
    """Recheck the vanished gateway authority and attempt its original request once."""

    repo_root: Path
    upstream_coordinator: str
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    quiet_stdout: bool = False
    timeout: float = COMMAND_TIMEOUT_SECONDS

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return list_herdr_agent_rows(self.env)

    def _dispatch_ops(
        self, preparation: ContinuationPreparation, root: Path
    ) -> HerdrSublaneActuatorOps:
        pointer = preparation.pointer
        pin = preparation.participant
        return HerdrSublaneActuatorOps(
            repo_root=root,
            lane_label=pin.lane_id,
            issue=pointer.issue_id,
            journal=pointer.journal_id,
            env=self.env,
            runner=self.runner,
            quiet_stdout=self.quiet_stdout,
            timeout=self.timeout,
        )

    @staticmethod
    def _row_root_is_exact(
        rows: object, *, assigned_name: str, root: Path
    ) -> tuple[bool, int]:
        if type(rows) not in (list, tuple):
            return False, -1
        matches = [
            row
            for row in rows
            if type(row) is dict
            and type(row.get(AGENT_KEY_NAME)) is str
            and row.get(AGENT_KEY_NAME) == assigned_name
        ]
        if len(matches) != 1:
            return False, -1
        row = matches[0]
        raw_paths = [
            row[key]
            for key in ("foreground_cwd", "cwd")
            if key in row and row[key] not in (None, "")
        ]
        if (
            not raw_paths
            or any(type(value) is not str or _plain(value) != value for value in raw_paths)
            or len(set(raw_paths)) != 1
        ):
            return False, -1
        raw_root = raw_paths[0]
        try:
            candidate = Path(raw_root)
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return False, -1
        revision = row.get("revision")
        if (
            not candidate.is_absolute()
            or candidate != resolved
            or resolved != root
            or type(revision) is not int
            or revision < 0
        ):
            return False, -1
        return True, revision

    def _authority(
        self,
        preparation: ContinuationPreparation,
        root: Path,
    ) -> Optional[VanishedGatewaySendAuthority]:
        try:
            rows = self._rows()
            inventory = resolve_vanished_gateway_inventory(
                preparation,
                repo_root=root,
                list_rows=lambda: rows,
            )
            evidence = verify_vanished_gateway_attestation_evidence(
                preparation,
                inventory,
                repo_root=root,
            )
        except (Exception, SystemExit):  # unreadable authority is a proven pre-send refusal
            return None
        if (
            type(inventory) is not VanishedGatewayInventoryJoin
            or not inventory.joined
            or type(evidence) is not VanishedGatewayAttestationEvidence
            or not evidence.bound
            or type(evidence.proof) is not VanishedGatewayAttestationProof
        ):
            return None
        proof = evidence.proof
        axes = (
            _plain(proof.action_id),
            _plain(proof.workspace_id),
            _plain(proof.lane_id),
            _plain(proof.provider),
            _plain(proof.assigned_name),
            _plain(proof.fresh_locator),
            _plain(proof.old_locator),
            _plain(evidence.observed_at),
        )
        if not all(axes):
            return None
        if axes[:7] != (
            inventory.action_id,
            inventory.workspace_id,
            inventory.lane_id,
            inventory.provider,
            inventory.assigned_name,
            inventory.fresh_locator,
            inventory.old_locator,
        ):
            return None
        root_ok, revision = self._row_root_is_exact(
            rows, assigned_name=inventory.assigned_name, root=root
        )
        if not root_ok:
            return None
        from .herdr_live_attestation_time import fresh_attestation_identity
        boundary = fresh_attestation_identity(
            home=None,
            rows=rows,
            assigned_name=inventory.assigned_name,
            workspace_id=inventory.workspace_id,
            role=inventory.provider,
            lane=inventory.lane_id,
        )
        if (
            boundary is None
            or boundary.locator != inventory.fresh_locator
            or boundary.observed_at != evidence.observed_at
            or boundary.row_revision != str(revision)
        ):
            return None
        return VanishedGatewaySendAuthority(
            action_id=axes[0],
            workspace_id=axes[1],
            lane_id=axes[2],
            provider=axes[3],
            assigned_name=axes[4],
            fresh_locator=axes[5],
            old_locator=axes[6],
            observed_at=axes[7],
            startup_action_id=boundary.startup_action_id,
            revision=revision,
        )

    def context_is_exact(self) -> bool:
        """Whether the immutable send context is canonical and explicit."""

        return (
            _canonical_root(self.repo_root) is not None
            and bool(_plain(self.upstream_coordinator))
        )

    def current_authority(
        self, preparation: object
    ) -> Optional[VanishedGatewaySendAuthority]:
        """Read one fresh inventory + attestation/action authority snapshot.

        This is intentionally read-only.  It exposes no caller-supplied locator, provider,
        anchor, inventory reader, or attestation home, and it never sends.  The continuation
        drain uses it both for the pre-existing-ledger check and for the action-time fence;
        :meth:`send_once` still performs its own two reads around the canonical call.
        """

        root = _canonical_root(self.repo_root)
        if (
            root is None
            or type(preparation) is not ContinuationPreparation
            or preparation.outcome != CONTINUATION_READY
        ):
            return None
        pointer = preparation.pointer
        pin = preparation.participant
        if (
            _plain(getattr(pointer, "source", None)) != "redmine"
            or not _plain(getattr(pointer, "issue_id", None))
            or not _plain(getattr(pointer, "journal_id", None))
            or _plain(getattr(pointer, "expected_gate", None)) != RESUME_GATE
            or _plain(getattr(pointer, "next_semantic_action", None))
            != REDISPATCH_GATEWAY_ONCE
            or not _plain(getattr(pin, "lane_id", None))
        ):
            return None
        return self._authority(preparation, root)

    def _send_once(
        self,
        preparation: object,
        *,
        expected_authority: object = None,
    ) -> VanishedGatewaySendResult:
        """Attempt once, optionally pinned to a caller's exact ledger authority."""

        if (
            expected_authority is not None
            and type(expected_authority) is not VanishedGatewaySendAuthority
        ):
            return _result(DRAIN_SEND_ZERO, SEND_AUTHORITY_INVALID)
        root = _canonical_root(self.repo_root)
        upstream = _plain(self.upstream_coordinator)
        if root is None or not self.context_is_exact():
            return _result(DRAIN_SEND_ZERO, SEND_AUTHORITY_INVALID)
        initial = self.current_authority(preparation)
        if initial is None:
            return _result(DRAIN_SEND_ZERO, SEND_AUTHORITY_INVALID)
        if expected_authority is not None and initial != expected_authority:
            return _result(DRAIN_SEND_ZERO, SEND_AUTHORITY_MOVED)
        pointer = preparation.pointer
        try:
            dispatch_ops = self._dispatch_ops(preparation, root)
        except (Exception, SystemExit):
            return _result(DRAIN_SEND_ZERO, SEND_AUTHORITY_INVALID)

        # The final external authority observation.  Never reuse ``initial`` as send
        # authority: a recycled locator, changed revision, or rewritten action binding
        # between the first check and this point is a known zero-send.  A continuation
        # additionally supplies the exact authority whose ledger was checked before its
        # attempted CAS; moving to another otherwise-valid generation cannot inherit that
        # earlier proof of ledger absence.
        current = self.current_authority(preparation)
        if (
            current is None
            or current != initial
            or (
                expected_authority is not None
                and current != expected_authority
            )
        ):
            return _result(DRAIN_SEND_ZERO, SEND_AUTHORITY_MOVED)
        try:
            rc = dispatch_ops.dispatch_implementation_request(
                issue=pointer.issue_id,
                journal=pointer.journal_id,
                gateway_pane=current.fresh_locator,
                lane_label=current.lane_id,
                upstream_coordinator=upstream,
                target_repo=str(root),
            )
        except (Exception, SystemExit):
            return _result(DRAIN_SEND_ERROR, SEND_FAILED)
        if type(rc) is not int or rc != 0:
            return _result(DRAIN_SEND_ERROR, SEND_FAILED)
        return VanishedGatewaySendResult(
            status=DRAIN_SEND_OK,
            detail=SEND_ATTEMPTED,
            action_id=current.action_id,
            workspace_id=current.workspace_id,
            lane_id=current.lane_id,
            provider=current.provider,
            assigned_name=current.assigned_name,
            fresh_locator=current.fresh_locator,
            old_locator=current.old_locator,
            observed_at=current.observed_at,
        )

    def send_once_for_authority(
        self,
        preparation: object,
        *,
        expected_authority: object,
    ) -> VanishedGatewaySendResult:
        """Attempt only for the exact authority used by the caller's ledger barrier.

        This is the continuation-drain entrypoint.  The expected authority is mandatory so
        a newly valid locator/revision cannot inherit another generation's readable-empty
        ledger observation.
        """

        if type(expected_authority) is not VanishedGatewaySendAuthority:
            return _result(DRAIN_SEND_ZERO, SEND_AUTHORITY_INVALID)
        return self._send_once(
            preparation,
            expected_authority=expected_authority,
        )

    def send_once(self, preparation: object) -> VanishedGatewaySendResult:
        """Attempt the original implementation request at most once.

        Every refusal before the canonical dispatch call is a proven zero-send.  Once that
        call is invoked, an exception or nonzero/unknown result has unknown delivery fate
        and is therefore ``DRAIN_SEND_ERROR``, never ``DRAIN_SEND_ZERO``.
        """

        return self._send_once(preparation)


__all__ = (
    "SEND_ATTEMPTED",
    "SEND_AUTHORITY_INVALID",
    "SEND_AUTHORITY_MOVED",
    "SEND_FAILED",
    "VanishedGatewayContinuationOps",
    "VanishedGatewaySendAuthority",
    "VanishedGatewaySendResult",
)
