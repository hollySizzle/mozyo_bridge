"""Fresh Redmine/issuer adapter for destructive recovery owner approvals."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    ResolvedIssuer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
    CONFIG_RELPATH,
    config_policy_pointer,
    resolve_journal_issuer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_owner_approval import (  # noqa: E501
    GATEWAY_RECOVERY_APPROVAL_EFFECT,
    GATEWAY_RECOVERY_APPROVAL_GATE,
    RecoveryOwnerApprovalError,
    STALE_WORKER_RECOVERY_APPROVAL_EFFECT,
    STALE_WORKER_RECOVERY_APPROVAL_GATE,
    gateway_recovery_approval_operation,
    stale_worker_recovery_approval_operation,
    verify_recovery_owner_approval,
)


def committed_issuer_policy_pointer(repo_root: Path) -> str:
    """Return the committed config blob authority, never working-tree bytes."""

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"HEAD:{CONFIG_RELPATH}"],
            text=True,
            capture_output=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    blob = result.stdout.strip()
    return config_policy_pointer(blob) if blob else ""


def resolved_recovery_approval_issuer(
    entry: object, *, repo_root: Path, issuer_resolver=None
) -> object:
    """Resolve the journal writer through the gate-specific durable policy."""

    if issuer_resolver is not None:
        try:
            return issuer_resolver(entry)
        except Exception:  # noqa: BLE001 - an unreadable authority is no authority
            return ResolvedIssuer()
    try:
        return resolve_journal_issuer(
            notes=str(getattr(entry, "notes", "") or ""),
            journal_id=str(getattr(entry, "journal_id", "") or ""),
            policy_pointer=committed_issuer_policy_pointer(repo_root),
        )
    except Exception:  # noqa: BLE001 - fail closed at the destructive boundary
        return ResolvedIssuer()


def verify_live_recovery_owner_approval(
    *,
    repo_root: Path,
    journal_reader,
    journal_reader_fresh: bool,
    journal: str,
    anchor_issue: str,
    gate: str,
    effect: str,
    issue: str,
    lane: str,
    operation: Mapping[str, object],
    issuer_resolver=None,
) -> bool:
    """Fresh-read and verify one recovery approval; every gap is ``False``."""

    if journal_reader is None or not journal_reader_fresh:
        return False
    try:
        entries = list(journal_reader(anchor_issue))
    except Exception:  # noqa: BLE001 - unreadable durable source never authorizes close
        return False
    exact = [
        entry
        for entry in entries
        if str(getattr(entry, "issue_id", "") or "").strip()
        == str(anchor_issue or "").strip()
        and str(getattr(entry, "journal_id", "") or "").strip()
        == str(journal or "").strip()
    ]
    issuer = (
        resolved_recovery_approval_issuer(
            exact[0], repo_root=repo_root, issuer_resolver=issuer_resolver
        )
        if len(exact) == 1
        else ResolvedIssuer()
    )
    try:
        verify_recovery_owner_approval(
            entries,
            journal=journal,
            anchor_issue=anchor_issue,
            issuer=issuer,
            gate=gate,
            effect=effect,
            issue=issue,
            lane=lane,
            operation=operation,
        )
    except RecoveryOwnerApprovalError:
        return False
    except Exception:  # noqa: BLE001 - malformed history is not an approval
        return False
    return True


def verify_live_gateway_recovery_approval(ops: object, request: object, journal: str) -> bool:
    """Bound adapter used by ``LiveGatewayRecoveryOps``."""

    return verify_live_recovery_owner_approval(
        repo_root=getattr(ops, "repo_root"),
        journal_reader=getattr(ops, "journal_reader", None),
        journal_reader_fresh=bool(getattr(ops, "journal_reader_fresh", False)),
        journal=journal,
        anchor_issue=getattr(request, "effective_anchor_issue", ""),
        gate=GATEWAY_RECOVERY_APPROVAL_GATE,
        effect=GATEWAY_RECOVERY_APPROVAL_EFFECT,
        issue=getattr(request, "issue", ""),
        lane=getattr(request, "lane", ""),
        operation=gateway_recovery_approval_operation(request),
        issuer_resolver=getattr(ops, "issuer_resolver", None),
    )


def verify_live_stale_worker_recovery_approval(
    ops: object, request: object, journal: str
) -> bool:
    """Bound adapter used by ``LiveStaleWorkerRecoveryOps``."""

    return verify_live_recovery_owner_approval(
        repo_root=getattr(ops, "repo_root"),
        journal_reader=getattr(ops, "journal_reader", None),
        journal_reader_fresh=bool(getattr(ops, "journal_reader_fresh", False)),
        journal=journal,
        anchor_issue=getattr(request, "issue", ""),
        gate=STALE_WORKER_RECOVERY_APPROVAL_GATE,
        effect=STALE_WORKER_RECOVERY_APPROVAL_EFFECT,
        issue=getattr(request, "issue", ""),
        lane=getattr(request, "lane", ""),
        operation=stale_worker_recovery_approval_operation(request),
        issuer_resolver=getattr(ops, "issuer_resolver", None),
    )


__all__ = (
    "committed_issuer_policy_pointer",
    "resolved_recovery_approval_issuer",
    "verify_live_recovery_owner_approval",
    "verify_live_gateway_recovery_approval",
    "verify_live_stale_worker_recovery_approval",
)
