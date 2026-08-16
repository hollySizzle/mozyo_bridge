"""Shared structured owner approval for destructive gateway/worker recovery.

Redmine #14663 found that ``recover-gateway`` and ``recover-stale`` accepted any
well-shaped non-empty journal pointer as an approval.  A pointer locates a record; it does
not prove that the record positively authorizes the requested destructive operation.

This module gives both recovery surfaces one approval grammar and one verifier.  The older
``worker_refresh_owner_approval`` surface keeps its public marker token and digest for
compatibility, but delegates its strict marker scan to the generic scanner defined here.
That is the explicit migration boundary: established marker bytes stay stable while all
destructive recovery readers share the same canonical, quote-aware, duplicate-rejecting
parser.
"""

from __future__ import annotations

import hashlib
from typing import Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.canonical_note_scan import (  # noqa: E501
    canonical_marker_bodies,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    ISSUER_COORDINATOR,
    ISSUER_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    RedmineJournalEntry,
)

GATEWAY_RECOVERY_APPROVAL_GATE = "gateway_recovery_owner_approval"
STALE_WORKER_RECOVERY_APPROVAL_GATE = "stale_worker_recovery_owner_approval"
RESTORED_PAIR_RECOVERY_APPROVAL_GATE = "restored_pair_recovery_owner_approval"
GENERATION_MISMATCH_DISPOSITION_APPROVAL_GATE = (
    "generation_mismatch_disposition_owner_approval"
)

GATEWAY_RECOVERY_APPROVAL_EFFECT = "gateway_close_relaunch_resume"
STALE_WORKER_RECOVERY_APPROVAL_EFFECT = "stale_worker_close_relaunch_resume"
RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT = "restored_pair_close_relaunch"
GENERATION_MISMATCH_DISPOSITION_APPROVAL_EFFECT = (
    "generation_mismatch_pending_discard_close_relaunch"
)

RECOVERY_APPROVAL_VERSION = "1"
RECOVERY_APPROVAL_SOURCE = "direct_owner"
RECOVERY_APPROVAL_DECISION = "approved"
RECOVERY_APPROVAL_AUTHORITY_ROLES: frozenset[str] = frozenset({ISSUER_COORDINATOR})

STRUCTURED_APPROVAL_FIELD_ORDER = (
    "gate",
    "version",
    "approval_source",
    "decision",
    "effect",
    "issue",
    "lane",
    "action_digest",
)

_RECOVERY_EFFECTS = {
    GATEWAY_RECOVERY_APPROVAL_GATE: GATEWAY_RECOVERY_APPROVAL_EFFECT,
    STALE_WORKER_RECOVERY_APPROVAL_GATE: STALE_WORKER_RECOVERY_APPROVAL_EFFECT,
    RESTORED_PAIR_RECOVERY_APPROVAL_GATE: RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT,
    GENERATION_MISMATCH_DISPOSITION_APPROVAL_GATE: (
        GENERATION_MISMATCH_DISPOSITION_APPROVAL_EFFECT
    ),
}


class StructuredOwnerApprovalError(ValueError):
    """A marker cannot be read as one canonical structured approval."""


class RecoveryOwnerApprovalError(ValueError):
    """The named journal does not approve this exact recovery operation."""


def parse_strict_owner_approval_markers(
    notes: str,
    *,
    gate: str,
    field_order: tuple[str, ...] = STRUCTURED_APPROVAL_FIELD_ORDER,
) -> list[dict[str, str]]:
    """Return canonical markers for ``gate`` and reject ambiguous marker bytes.

    The scan is quote/code-fence aware.  Duplicate fields, empty fragments, unknown fields,
    or a non-canonical field order fail closed instead of being normalized by a
    last-write-wins mapping.
    """

    gate_s = str(gate or "").strip()
    if not gate_s:
        raise StructuredOwnerApprovalError("an approval gate is required")
    parsed: list[dict[str, str]] = []
    for _channel, body in canonical_marker_bodies(
        notes, channels=frozenset({MARKER_CHANNEL_WORKFLOW_EVENT})
    ):
        components = body.split(":")
        if not any(component.strip() == f"gate={gate_s}" for component in components):
            continue
        fields: dict[str, str] = {}
        order: list[str] = []
        for component in components:
            key, separator, value = component.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not key or not value:
                raise StructuredOwnerApprovalError(
                    "the approval marker carries a malformed field "
                    "(not a non-empty key=value pair)"
                )
            if key in fields:
                raise StructuredOwnerApprovalError(
                    f"the approval marker declares {key!r} more than once; a record that "
                    "says two things has decided nothing"
                )
            fields[key] = value
            order.append(key)
        if tuple(order) != field_order:
            raise StructuredOwnerApprovalError(
                "the approval marker's field sequence is not the canonical one"
            )
        parsed.append(fields)
    return parsed


def _operation_value(name: str, value: object) -> str:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, int):
        rendered = str(value)
    else:
        rendered = str(value or "").strip()
    if not rendered:
        raise RecoveryOwnerApprovalError(
            f"a recovery approval requires a non-empty {name}"
        )
    if "\n" in rendered or "\r" in rendered:
        raise RecoveryOwnerApprovalError(
            f"a recovery approval field may not contain a newline ({name})"
        )
    return rendered


def recovery_approval_digest(
    *, gate: str, effect: str, operation: Mapping[str, object]
) -> str:
    """Fingerprint every authority-bearing field of one recovery operation."""

    gate_s = str(gate or "").strip()
    effect_s = str(effect or "").strip()
    if _RECOVERY_EFFECTS.get(gate_s) != effect_s:
        raise RecoveryOwnerApprovalError("unknown recovery approval gate/effect pair")
    if not operation:
        raise RecoveryOwnerApprovalError("a recovery approval requires operation fields")
    normalized = {
        str(name or "").strip(): _operation_value(str(name or "").strip(), value)
        for name, value in operation.items()
    }
    if not all(normalized) or len(normalized) != len(operation):
        raise RecoveryOwnerApprovalError(
            "a recovery approval operation contains an empty or duplicate field name"
        )
    generation = normalized.get("action_generation", "")
    if not generation.isascii() or not generation.isdecimal() or int(generation) < 1:
        raise RecoveryOwnerApprovalError(
            "a recovery approval requires a positive integer action_generation"
        )
    encoded = "\n".join(
        [
            f"gate\t{gate_s}",
            f"version\t{RECOVERY_APPROVAL_VERSION}",
            f"effect\t{effect_s}",
        ]
        + [f"{name}\t{normalized[name]}" for name in sorted(normalized)]
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def gateway_recovery_approval_operation(request: object) -> dict[str, object]:
    """All gateway-refresh authorities that one approval must pin."""

    effective_anchor_issue = getattr(request, "effective_anchor_issue", "")
    return {
        "action_id": getattr(request, "action_id", ""),
        "action_generation": getattr(request, "action_generation", 0),
        "role": getattr(request, "role", ""),
        "provider": getattr(request, "provider", ""),
        "assigned_name": getattr(request, "assigned_name", ""),
        "locator": getattr(request, "locator", ""),
        "participant_revision": getattr(request, "gateway_revision", ""),
        "lane_revision": getattr(request, "lane_revision", ""),
        "lane_generation": getattr(request, "lane_generation", ""),
        "anchor_issue": effective_anchor_issue,
        "resume_anchor_journal": getattr(request, "resume_anchor_journal", ""),
        "resume_gate": getattr(request, "resume_gate", ""),
    }


def stale_worker_recovery_approval_operation(request: object) -> dict[str, object]:
    """All vanished-worker recovery authorities that one approval must pin."""

    return {
        "action_id": getattr(request, "action_id", ""),
        "action_generation": getattr(request, "action_generation", 0),
        "role": getattr(request, "role", ""),
        "provider": getattr(request, "provider", ""),
        "assigned_name": getattr(request, "assigned_name", ""),
        "locator": getattr(request, "locator", ""),
        "participant_revision": getattr(request, "worker_revision", ""),
        "lane_revision": getattr(request, "lane_revision", ""),
        "lane_generation": getattr(request, "lane_generation", ""),
        "anchor_issue": getattr(request, "issue", ""),
        "expected_gate": getattr(request, "expected_gate", ""),
        "next_semantic_action": getattr(request, "next_semantic_action", ""),
        "supersede": bool(getattr(request, "supersede", False)),
    }


def expected_recovery_approval_fields(
    *,
    gate: str,
    effect: str,
    issue: str,
    lane: str,
    operation: Mapping[str, object],
) -> dict[str, str]:
    issue_s = _operation_value("issue", issue)
    lane_s = _operation_value("lane", lane)
    return {
        "gate": str(gate or "").strip(),
        "version": RECOVERY_APPROVAL_VERSION,
        "approval_source": RECOVERY_APPROVAL_SOURCE,
        "decision": RECOVERY_APPROVAL_DECISION,
        "effect": str(effect or "").strip(),
        "issue": issue_s,
        "lane": lane_s,
        "action_digest": recovery_approval_digest(
            gate=gate, effect=effect, operation=operation
        ),
    }


def render_recovery_owner_approval_marker(**approval: object) -> str:
    """Render the one marker that can approve the supplied recovery operation."""

    fields = expected_recovery_approval_fields(**approval)  # type: ignore[arg-type]
    body = ":".join(
        f"{key}={fields[key]}" for key in STRUCTURED_APPROVAL_FIELD_ORDER
    )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


def verify_recovery_owner_approval(
    entries: Sequence[RedmineJournalEntry],
    *,
    journal: str,
    anchor_issue: str,
    issuer: object,
    gate: str,
    effect: str,
    issue: str,
    lane: str,
    operation: Mapping[str, object],
) -> Mapping[str, str]:
    """Verify one fresh, uniquely-owned, coordinator-recorded direct-owner approval."""

    journal_s = str(journal or "").strip()
    anchor_issue_s = str(anchor_issue or "").strip()
    if not journal_s or not anchor_issue_s:
        raise RecoveryOwnerApprovalError(
            "an approval journal and its owning issue are required"
        )
    expected = expected_recovery_approval_fields(
        gate=gate, effect=effect, issue=issue, lane=lane, operation=operation
    )
    exact = [
        entry
        for entry in entries
        if str(getattr(entry, "issue_id", "") or "").strip() == anchor_issue_s
        and str(getattr(entry, "journal_id", "") or "").strip() == journal_s
    ]
    if len(exact) != 1:
        raise RecoveryOwnerApprovalError(
            "the exact Redmine approval journal does not exist uniquely on the named issue"
        )

    role = str(getattr(issuer, "role", "") or "").strip()
    anchored = bool(getattr(issuer, "is_anchored", False))
    if not role or role == ISSUER_UNKNOWN or not anchored:
        raise RecoveryOwnerApprovalError(
            "the approval issuer is not resolved to an anchored authority"
        )
    if role not in RECOVERY_APPROVAL_AUTHORITY_ROLES:
        raise RecoveryOwnerApprovalError(
            "the approval journal writer does not hold owner-approval authority"
        )

    try:
        candidates = parse_strict_owner_approval_markers(
            str(getattr(exact[0], "notes", "") or ""), gate=gate
        )
    except StructuredOwnerApprovalError as exc:
        raise RecoveryOwnerApprovalError(str(exc)) from exc
    if len(candidates) != 1:
        raise RecoveryOwnerApprovalError(
            "the exact journal does not contain one structured recovery owner approval"
        )
    fields = candidates[0]
    if set(fields) != set(STRUCTURED_APPROVAL_FIELD_ORDER):
        raise RecoveryOwnerApprovalError(
            "the approval marker's field set is not the canonical one"
        )
    wrong = [key for key, value in expected.items() if fields.get(key) != value]
    if wrong:
        raise RecoveryOwnerApprovalError(
            "the structured owner approval targets another operation, round or lane "
            f"(mismatched fields: {', '.join(sorted(wrong))})"
        )
    return dict(fields)


__all__ = (
    "GATEWAY_RECOVERY_APPROVAL_GATE",
    "STALE_WORKER_RECOVERY_APPROVAL_GATE",
    "RESTORED_PAIR_RECOVERY_APPROVAL_GATE",
    "GENERATION_MISMATCH_DISPOSITION_APPROVAL_GATE",
    "GATEWAY_RECOVERY_APPROVAL_EFFECT",
    "STALE_WORKER_RECOVERY_APPROVAL_EFFECT",
    "RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT",
    "GENERATION_MISMATCH_DISPOSITION_APPROVAL_EFFECT",
    "RECOVERY_APPROVAL_VERSION",
    "RECOVERY_APPROVAL_SOURCE",
    "RECOVERY_APPROVAL_DECISION",
    "RECOVERY_APPROVAL_AUTHORITY_ROLES",
    "STRUCTURED_APPROVAL_FIELD_ORDER",
    "StructuredOwnerApprovalError",
    "RecoveryOwnerApprovalError",
    "parse_strict_owner_approval_markers",
    "recovery_approval_digest",
    "gateway_recovery_approval_operation",
    "stale_worker_recovery_approval_operation",
    "expected_recovery_approval_fields",
    "render_recovery_owner_approval_marker",
    "verify_recovery_owner_approval",
)
