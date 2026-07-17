"""Exact owner approval for discarding pending composer input (Redmine #13918).

The CLI's ``ISSUE:JOURNAL`` value is only a locator.  Authority comes from a fresh
credentialed read of that exact journal and one structured marker that binds the approval to
the exact scratch-pair identity observed at action time.  Prose is never interpreted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MARKER_CHANNEL_WORKFLOW_EVENT,
    RedmineJournalEntry,
    marker_fields_in_note,
)

APPROVAL_GATE = "pending_composer_discard_approval"
APPROVAL_EFFECT = "discard_pending_composer_and_retire"
APPROVAL_SOURCE = "direct_owner"
APPROVAL_DECISION = "approved"
APPROVAL_VERSION = "1"


class ComposerDiscardApprovalError(ValueError):
    """The live approval is missing, ambiguous, malformed, or targets another pair."""


def pin_digest(pinned: Sequence[tuple[str, str]]) -> str:
    """Canonical fingerprint of the exact role/locator pair the approval authorizes."""
    rows = sorted({(str(role).strip(), str(locator).strip()) for role, locator in pinned})
    if not rows or any(not role or not locator for role, locator in rows):
        raise ComposerDiscardApprovalError(
            "composer-discard approval requires a positively identified live/pending pair"
        )
    encoded = "\n".join(f"{role}\t{locator}" for role, locator in rows)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ComposerDiscardApprovalEvidence:
    """Canonical evidence persisted in the load-bearing retirement attempt."""

    issue: str
    journal: str
    workspace_id: str
    lane_id: str
    slot_digest: str
    pin_digest: str
    notes_sha256: str
    version: str = APPROVAL_VERSION

    @property
    def token(self) -> str:
        return f"{self.issue}:{self.journal}"

    def as_payload(self) -> dict[str, str]:
        return {
            "version": self.version,
            "issue": self.issue,
            "journal": self.journal,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "slot_digest": self.slot_digest,
            "pin_digest": self.pin_digest,
            "notes_sha256": self.notes_sha256,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    @classmethod
    def from_json(cls, value: str) -> "ComposerDiscardApprovalEvidence":
        try:
            payload = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ComposerDiscardApprovalError(
                "stored composer-discard approval evidence is unreadable"
            ) from exc
        if not isinstance(payload, dict):
            raise ComposerDiscardApprovalError(
                "stored composer-discard approval evidence is not an object"
            )
        expected = {
            "version",
            "issue",
            "journal",
            "workspace_id",
            "lane_id",
            "slot_digest",
            "pin_digest",
            "notes_sha256",
        }
        if set(payload) != expected or any(
            not isinstance(payload.get(key), str) or not payload.get(key)
            for key in expected
        ):
            raise ComposerDiscardApprovalError(
                "stored composer-discard approval evidence has an invalid schema"
            )
        evidence = cls(**payload)
        if evidence.version != APPROVAL_VERSION:
            raise ComposerDiscardApprovalError(
                "stored composer-discard approval evidence has an unknown version"
            )
        if evidence.canonical_json() != value:
            raise ComposerDiscardApprovalError(
                "stored composer-discard approval evidence is not canonical"
            )
        return evidence


def verify_composer_discard_approval(
    entries: Sequence[RedmineJournalEntry],
    *,
    issue: str,
    journal: str,
    workspace_id: str,
    lane_id: str,
    slot_digest: str,
    pinned: Sequence[tuple[str, str]],
) -> ComposerDiscardApprovalEvidence:
    """Verify one exact structured approval from a freshly fetched issue history."""
    exact = [
        entry
        for entry in entries
        if entry.issue_id == issue and entry.journal_id == journal
    ]
    if len(exact) != 1:
        raise ComposerDiscardApprovalError(
            "the exact Redmine approval journal does not exist uniquely on the named issue"
        )
    entry = exact[0]
    candidates = [
        fields
        for channel, fields in marker_fields_in_note(entry.notes)
        if channel == MARKER_CHANNEL_WORKFLOW_EVENT
        and fields.get("gate") == APPROVAL_GATE
    ]
    if len(candidates) != 1:
        raise ComposerDiscardApprovalError(
            "the exact journal does not contain one structured composer-discard owner approval"
        )
    fields = candidates[0]
    expected = {
        "gate": APPROVAL_GATE,
        "version": APPROVAL_VERSION,
        "approval_source": APPROVAL_SOURCE,
        "decision": APPROVAL_DECISION,
        "effect": APPROVAL_EFFECT,
        "issue": issue,
        "workspace": workspace_id,
        "lane": lane_id,
        "slot_digest": slot_digest,
        "pin_digest": pin_digest(pinned),
    }
    wrong = [key for key, value in expected.items() if fields.get(key) != value]
    if wrong:
        raise ComposerDiscardApprovalError(
            "the structured owner approval targets another operation or pair "
            f"(mismatched fields: {', '.join(sorted(wrong))})"
        )
    return ComposerDiscardApprovalEvidence(
        issue=issue,
        journal=journal,
        workspace_id=workspace_id,
        lane_id=lane_id,
        slot_digest=slot_digest,
        pin_digest=expected["pin_digest"],
        notes_sha256=hashlib.sha256(entry.notes.encode("utf-8")).hexdigest(),
    )


__all__ = (
    "APPROVAL_GATE",
    "APPROVAL_EFFECT",
    "APPROVAL_SOURCE",
    "APPROVAL_DECISION",
    "APPROVAL_VERSION",
    "ComposerDiscardApprovalError",
    "ComposerDiscardApprovalEvidence",
    "pin_digest",
    "verify_composer_discard_approval",
)
