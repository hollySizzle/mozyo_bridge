"""Typed contract for an operator-owned recovery-anchor delivery.

This boundary deliberately does not expose a free-form body or role.  A caller
may request only one of the two closed recovery handoff kinds, pinned to one
exact live receiver generation and one startup action attestation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable


KIND_REPLY = "reply"
KIND_IMPLEMENTATION_REQUEST = "implementation_request"
RECOVERY_ANCHOR_DELIVERY_KINDS: frozenset[str] = frozenset(
    {KIND_REPLY, KIND_IMPLEMENTATION_REQUEST}
)

RECOVERY_DELIVERY_AUTHORIZATION_CHANNEL = "recovery-delivery-authorization"
RECOVERY_DELIVERY_AUTHORIZED = "authorized"
RECOVERY_DELIVERY_AUTHORIZER_OWNER = "owner"
RECOVERY_DELIVERY_PRIOR_ZERO_SEND = "known_zero_send"

_AUTHORIZATION_RE = re.compile(
    r"\[mozyo:recovery-delivery-authorization:(?P<body>[^\]]*)\]"
)
_AUTHORIZATION_FIELDS = frozenset(
    {
        "conclusion",
        "authorized_by_role",
        "issue",
        "lane",
        "workspace_id",
        "anchor_journal",
        "retry_of_action_sha256",
        "prior_zero_send_journal",
        "prior_outcome",
        "typed_count",
        "send_count",
        "turn_start_count",
        "target_count",
    }
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
class RecoveryDeliveryAuthorization:
    """One strict owner authorization read from its own durable journal entry."""

    journal: str
    conclusion: str
    authorized_by_role: str
    issue: str
    lane: str
    workspace_id: str
    anchor_journal: str
    retry_of_action_sha256: str
    prior_zero_send_journal: str
    prior_outcome: str
    typed_count: str
    send_count: str
    turn_start_count: str
    target_count: str

    def valid_for(
        self,
        *,
        issue: object,
        lane: object,
        workspace_id: object,
        approval_journal: object,
        anchor_journal: object,
        retry_of_action_id: object,
        prior_zero_send_journal: object,
    ) -> bool:
        """Whether every action and known-zero-send axis is exact."""

        return bool(
            self.journal == _norm(approval_journal)
            and self.conclusion == RECOVERY_DELIVERY_AUTHORIZED
            and self.authorized_by_role == RECOVERY_DELIVERY_AUTHORIZER_OWNER
            and self.issue == _norm(issue)
            and self.lane == _norm(lane)
            and self.workspace_id == _norm(workspace_id)
            and self.anchor_journal == _norm(anchor_journal)
            and self.retry_of_action_sha256
            == hashlib.sha256(_norm(retry_of_action_id).encode("utf-8")).hexdigest()
            and self.prior_zero_send_journal == _norm(prior_zero_send_journal)
            and self.prior_outcome == RECOVERY_DELIVERY_PRIOR_ZERO_SEND
            and self.typed_count == "0"
            and self.send_count == "0"
            and self.turn_start_count == "0"
            and self.target_count == "0"
        )


def parse_recovery_delivery_authorizations(
    entries: Iterable[object],
) -> tuple[RecoveryDeliveryAuthorization, ...]:
    """Parse strict action-specific authorization markers, never surrounding prose.

    Duplicate fields, missing/extra fields, empty values, malformed tokens, or
    more than one marker in a journal entry all fail closed for that entry.
    """

    found: list[RecoveryDeliveryAuthorization] = []
    for entry in entries:
        journal = _norm(getattr(entry, "journal_id", ""))
        notes = getattr(entry, "notes", "")
        if not journal or not isinstance(notes, str):
            continue
        matches = tuple(_AUTHORIZATION_RE.finditer(notes))
        if len(matches) != 1:
            continue
        fields: dict[str, str] = {}
        valid = True
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
                or key not in _AUTHORIZATION_FIELDS
                or any(character.isspace() for character in key)
            ):
                valid = False
                break
            fields[key] = value
        if not valid or frozenset(fields) != _AUTHORIZATION_FIELDS:
            continue
        found.append(
            RecoveryDeliveryAuthorization(
                journal=journal,
                conclusion=fields["conclusion"],
                authorized_by_role=fields["authorized_by_role"],
                issue=fields["issue"],
                lane=fields["lane"],
                workspace_id=fields["workspace_id"],
                anchor_journal=fields["anchor_journal"],
                retry_of_action_sha256=fields["retry_of_action_sha256"],
                prior_zero_send_journal=fields["prior_zero_send_journal"],
                prior_outcome=fields["prior_outcome"],
                typed_count=fields["typed_count"],
                send_count=fields["send_count"],
                turn_start_count=fields["turn_start_count"],
                target_count=fields["target_count"],
            )
        )
    return tuple(found)


def build_recovery_delivery_authorization_marker(
    *,
    issue: object,
    lane: object,
    workspace_id: object,
    anchor_journal: object,
    retry_of_action_id: object,
    prior_zero_send_journal: object,
) -> str:
    """Build the only machine-readable marker accepted by the live retry rail."""

    fields = (
        ("conclusion", RECOVERY_DELIVERY_AUTHORIZED),
        ("authorized_by_role", RECOVERY_DELIVERY_AUTHORIZER_OWNER),
        ("issue", _norm(issue)),
        ("lane", _norm(lane)),
        ("workspace_id", _norm(workspace_id)),
        ("anchor_journal", _norm(anchor_journal)),
        (
            "retry_of_action_sha256",
            hashlib.sha256(_norm(retry_of_action_id).encode("utf-8")).hexdigest()
            if _norm(retry_of_action_id)
            else "",
        ),
        ("prior_zero_send_journal", _norm(prior_zero_send_journal)),
        ("prior_outcome", RECOVERY_DELIVERY_PRIOR_ZERO_SEND),
        ("typed_count", "0"),
        ("send_count", "0"),
        ("turn_start_count", "0"),
        ("target_count", "0"),
    )
    if any(not value for _key, value in fields):
        raise ValueError("recovery delivery authorization requires every exact authority field")
    return (
        f"[mozyo:{RECOVERY_DELIVERY_AUTHORIZATION_CHANNEL}:"
        + ":".join(f"{key}={value}" for key, value in fields)
        + "]"
    )


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
class RecoveryAnchorDeliveryPreflight:
    """Read-only delivery admission; never claims that a turn started."""

    may_deliver: bool
    detail: str
    marker: str = ""

    def __post_init__(self) -> None:
        if self.detail not in RECOVERY_ANCHOR_DELIVERY_DETAILS:
            raise ValueError(f"unsupported recovery delivery detail {self.detail!r}")


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
    "RECOVERY_DELIVERY_AUTHORIZATION_CHANNEL",
    "RECOVERY_DELIVERY_AUTHORIZED",
    "RECOVERY_DELIVERY_AUTHORIZER_OWNER",
    "RECOVERY_DELIVERY_PRIOR_ZERO_SEND",
    "RecoveryDeliveryAuthorization",
    "RecoveryAnchorDeliveryPreflight",
    "build_recovery_delivery_authorization_marker",
    "parse_recovery_delivery_authorizations",
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
