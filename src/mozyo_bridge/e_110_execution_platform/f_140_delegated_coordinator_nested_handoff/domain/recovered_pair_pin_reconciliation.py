"""Pure request/outcome model for recovered active-pair pin reconciliation (#14203 R19)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    marker_fields_in_note,
)


def _norm(value: object) -> str:
    return str(value).strip() if value is not None else ""


def recovery_action_digest(value: object) -> str:
    action = _norm(value)
    return hashlib.sha256(action.encode("utf-8")).hexdigest() if action else ""


@dataclass(frozen=True)
class RecoveredPairPinReconciliationRequest:
    issue: str
    lane: str
    journal: str
    lifecycle_decision_journal: str
    target_action_id: str
    source_revision: int
    expected_revision: int
    lane_generation: int
    worktree: str

    @property
    def complete(self) -> bool:
        return bool(
            all(
                _norm(value)
                for value in (
                    self.issue,
                    self.lane,
                    self.journal,
                    self.lifecycle_decision_journal,
                    self.target_action_id,
                    self.worktree,
                )
            )
            and self.source_revision > 0
            and self.expected_revision > 0
            and self.lane_generation > 0
        )


def is_exact_reconciliation_authority(
    entry: object, request: RecoveredPairPinReconciliationRequest
) -> bool:
    """Require one exact structured owner-approval marker on the requested journal."""

    if (
        _norm(getattr(entry, "issue_id", "")) != _norm(request.issue)
        or _norm(getattr(entry, "journal_id", "")) != _norm(request.journal)
    ):
        return False
    expected = {
        "gate": "owner_approval",
        "kind": "recovered_pair_pin_reconciliation",
        "issue": _norm(request.issue),
        "lane": _norm(request.lane),
        "lane_generation": str(request.lane_generation),
        "source_revision": str(request.source_revision),
        "expected_revision": str(request.expected_revision),
        "lifecycle_decision_journal": _norm(
            request.lifecycle_decision_journal
        ),
        # Marker fields are colon-delimited, while recover-pair action ids contain
        # colons. Bind the exact opaque token by digest instead of truncating it.
        "target_action_digest": recovery_action_digest(
            request.target_action_id
        ),
    }
    matches = 0
    for channel, fields in marker_fields_in_note(
        _norm(getattr(entry, "notes", ""))
    ):
        if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
            continue
        if all(_norm(fields.get(key)) == value for key, value in expected.items()):
            matches += 1
    return matches == 1


@dataclass(frozen=True)
class RecoveredPairPinReconciliationPreflight:
    ready: bool
    detail: str
    workspace_id: str = ""
    old_locators: tuple[str, str] = ()
    recovered_locators: tuple[str, str] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "old_locators": list(self.old_locators),
            "recovered_locators": list(self.recovered_locators),
        }


@dataclass(frozen=True)
class RecoveredPairPinReconciliationOutcome:
    executed: bool
    issue: str
    lane: str
    preflight: RecoveredPairPinReconciliationPreflight
    applied: bool = False
    revision: int | None = None
    detail: str = ""

    @property
    def is_blocked(self) -> bool:
        if not self.preflight.ready:
            return True
        return self.executed and not self.applied

    def as_payload(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "issue": self.issue,
            "lane": self.lane,
            "preflight": self.preflight.as_payload(),
            "applied": self.applied,
            "revision": self.revision,
            "is_blocked": self.is_blocked,
            "detail": self.detail,
        }


__all__ = (
    "RecoveredPairPinReconciliationOutcome",
    "RecoveredPairPinReconciliationPreflight",
    "RecoveredPairPinReconciliationRequest",
    "is_exact_reconciliation_authority",
    "recovery_action_digest",
)
