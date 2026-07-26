"""Typed contract for an operator-owned recovery-anchor delivery.

This boundary deliberately does not expose a free-form body or role.  A caller
may request only one of the two closed recovery handoff kinds, pinned to one
exact live receiver generation and one startup action attestation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


KIND_REPLY = "reply"
KIND_IMPLEMENTATION_REQUEST = "implementation_request"
RECOVERY_ANCHOR_DELIVERY_KINDS: frozenset[str] = frozenset(
    {KIND_REPLY, KIND_IMPLEMENTATION_REQUEST}
)

DISPOSITION_STARTED = "started"
DISPOSITION_ZERO_SEND = "zero_send"
DISPOSITION_UNCERTAIN = "uncertain"
RECOVERY_ANCHOR_DELIVERY_DISPOSITIONS: frozenset[str] = frozenset(
    {DISPOSITION_STARTED, DISPOSITION_ZERO_SEND, DISPOSITION_UNCERTAIN}
)

DETAIL_OK = "ok"
DETAIL_INVALID_REQUEST = "invalid_request"
DETAIL_RAIL_UNAVAILABLE = "rail_unavailable"
DETAIL_WORKSPACE_MISMATCH = "workspace_mismatch"
DETAIL_TARGET_UNRESOLVED = "target_unresolved"
DETAIL_TARGET_IDENTITY_MISMATCH = "target_identity_mismatch"
DETAIL_TARGET_NOT_LIVE = "target_not_live"
DETAIL_TARGET_NOT_SETTLED = "target_not_settled"
DETAIL_TARGET_REVISION_MISMATCH = "target_revision_mismatch"
DETAIL_ATTESTATION_UNREADABLE = "attestation_unreadable"
DETAIL_ATTESTATION_MISMATCH = "attestation_mismatch"
DETAIL_TARGET_RETIRING = "target_retiring"
DETAIL_PRECONDITION_NOT_IDLE = "precondition_not_idle"
DETAIL_TURN_START_UNCONFIRMED = "turn_start_unconfirmed"
RECOVERY_ANCHOR_DELIVERY_DETAILS: frozenset[str] = frozenset(
    {
        DETAIL_OK,
        DETAIL_INVALID_REQUEST,
        DETAIL_RAIL_UNAVAILABLE,
        DETAIL_WORKSPACE_MISMATCH,
        DETAIL_TARGET_UNRESOLVED,
        DETAIL_TARGET_IDENTITY_MISMATCH,
        DETAIL_TARGET_NOT_LIVE,
        DETAIL_TARGET_NOT_SETTLED,
        DETAIL_TARGET_REVISION_MISMATCH,
        DETAIL_ATTESTATION_UNREADABLE,
        DETAIL_ATTESTATION_MISMATCH,
        DETAIL_TARGET_RETIRING,
        DETAIL_PRECONDITION_NOT_IDLE,
        DETAIL_TURN_START_UNCONFIRMED,
    }
)


def _norm(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def recovery_delivery_action_id(
    *,
    issue: object,
    lane: object,
    approval_journal: object,
    anchor_journal: object,
    retry_of_action_id: object,
    prior_zero_send_journal: object,
) -> str:
    """Return the stable id for one explicitly approved recovery retry.

    Every authority component is mandatory.  Canonical JSON plus SHA-256 avoids
    delimiter ambiguity while keeping the result stable across processes.
    """

    fields = {
        "issue": _norm(issue),
        "lane": _norm(lane),
        "approval_journal": _norm(approval_journal),
        "anchor_journal": _norm(anchor_journal),
        "retry_of_action_id": _norm(retry_of_action_id),
        "prior_zero_send_journal": _norm(prior_zero_send_journal),
    }
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise ValueError(
            "a recovery delivery action id requires every authority field "
            f"(missing: {', '.join(missing)})"
        )
    encoded = json.dumps(
        fields,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "recovery-delivery-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecoveryAnchorDeliveryRequest:
    issue: str
    journal: str
    kind: str
    workspace_id: str
    lane_id: str
    provider: str
    target_assigned_name: str
    target_locator: str
    target_revision: str
    target_action_id: str

    def __post_init__(self) -> None:
        if self.kind not in RECOVERY_ANCHOR_DELIVERY_KINDS:
            raise ValueError(
                f"unsupported recovery delivery kind {self.kind!r}; "
                f"allowed: {sorted(RECOVERY_ANCHOR_DELIVERY_KINDS)}"
            )
        missing = [
            name
            for name in (
                "issue",
                "journal",
                "workspace_id",
                "lane_id",
                "provider",
                "target_assigned_name",
                "target_locator",
                "target_revision",
                "target_action_id",
            )
            if not _norm(getattr(self, name))
        ]
        if missing:
            raise ValueError(
                "recovery anchor delivery requires an exact immutable target "
                f"(missing: {', '.join(missing)})"
            )


@dataclass(frozen=True)
class RecoveryAnchorDeliveryOutcome:
    disposition: str
    detail: str
    marker: str = ""
    turn_start_outcome: str = ""

    def __post_init__(self) -> None:
        if self.disposition not in RECOVERY_ANCHOR_DELIVERY_DISPOSITIONS:
            raise ValueError(f"unsupported recovery delivery disposition {self.disposition!r}")
        if self.detail not in RECOVERY_ANCHOR_DELIVERY_DETAILS:
            raise ValueError(f"unsupported recovery delivery detail {self.detail!r}")

    @property
    def started(self) -> bool:
        return self.disposition == DISPOSITION_STARTED

    @property
    def zero_send(self) -> bool:
        return self.disposition == DISPOSITION_ZERO_SEND

    @property
    def uncertain(self) -> bool:
        return self.disposition == DISPOSITION_UNCERTAIN

    def as_payload(self) -> dict[str, str]:
        return {
            "disposition": self.disposition,
            "detail": self.detail,
            "marker": self.marker,
            "turn_start_outcome": self.turn_start_outcome,
        }


__all__ = [
    "DETAIL_ATTESTATION_MISMATCH",
    "DETAIL_ATTESTATION_UNREADABLE",
    "DETAIL_INVALID_REQUEST",
    "DETAIL_OK",
    "DETAIL_PRECONDITION_NOT_IDLE",
    "DETAIL_RAIL_UNAVAILABLE",
    "DETAIL_TARGET_IDENTITY_MISMATCH",
    "DETAIL_TARGET_NOT_LIVE",
    "DETAIL_TARGET_NOT_SETTLED",
    "DETAIL_TARGET_RETIRING",
    "DETAIL_TARGET_REVISION_MISMATCH",
    "DETAIL_TARGET_UNRESOLVED",
    "DETAIL_TURN_START_UNCONFIRMED",
    "DETAIL_WORKSPACE_MISMATCH",
    "DISPOSITION_STARTED",
    "DISPOSITION_UNCERTAIN",
    "DISPOSITION_ZERO_SEND",
    "KIND_IMPLEMENTATION_REQUEST",
    "KIND_REPLY",
    "RECOVERY_ANCHOR_DELIVERY_DETAILS",
    "RECOVERY_ANCHOR_DELIVERY_DISPOSITIONS",
    "RECOVERY_ANCHOR_DELIVERY_KINDS",
    "RecoveryAnchorDeliveryOutcome",
    "RecoveryAnchorDeliveryRequest",
    "recovery_delivery_action_id",
]
