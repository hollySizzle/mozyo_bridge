"""Pure request/outcome model for recovered active-pair pin reconciliation (#14203 R19)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
)


_AUTHORITY_FIELDS = frozenset(
    {
        "gate",
        "kind",
        "issue",
        "lane",
        "lane_generation",
        "source_revision",
        "expected_revision",
        "lifecycle_decision_journal",
        "target_action_digest",
    }
)
_AUTHORITY_RE = re.compile(
    rf"\[mozyo:{re.escape(MARKER_CHANNEL_WORKFLOW_EVENT)}:(?P<body>[^\]]*)\]"
)


def _norm(value: object) -> str:
    return str(value).strip() if value is not None else ""


def recovery_action_digest(value: object) -> str:
    action = _norm(value)
    return hashlib.sha256(action.encode("utf-8")).hexdigest() if action else ""


def _strict_authority_fields(notes: object) -> dict[str, str] | None:
    """Parse exactly one closed R19 owner marker without lossy field folding."""

    if not isinstance(notes, str):
        return None
    matches = tuple(_AUTHORITY_RE.finditer(notes))
    if len(matches) != 1:
        return None
    fields: dict[str, str] = {}
    for raw_token in matches[0].group("body").split(":"):
        token = raw_token.strip()
        key, separator, value = token.partition("=")
        key = key.strip()
        value = value.strip()
        if (
            not separator
            or not key
            or not value
            or key in fields
            or key not in _AUTHORITY_FIELDS
            or any(character.isspace() for character in key)
        ):
            return None
        fields[key] = value
    return fields if frozenset(fields) == _AUTHORITY_FIELDS else None


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
    fields = _strict_authority_fields(getattr(entry, "notes", ""))
    return fields is not None and fields == expected


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
