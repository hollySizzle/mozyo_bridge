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

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    strict_marker_body_fields,
)


KIND_REPLY = "reply"
KIND_IMPLEMENTATION_REQUEST = "implementation_request"
RECOVERY_ANCHOR_DELIVERY_KINDS: frozenset[str] = frozenset(
    {KIND_REPLY, KIND_IMPLEMENTATION_REQUEST}
)

RECOVERY_DELIVERY_AUTHORIZATION_CHANNEL = "recovery-delivery-authorization"
RECOVERY_DELIVERY_ZERO_SEND_CHANNEL = "recovery-delivery-zero-send"
RECOVERY_DELIVERY_AUTHORIZED = "authorized"
RECOVERY_DELIVERY_AUTHORIZER_OWNER = "owner"
RECOVERY_DELIVERY_PRIOR_ZERO_SEND = "known_zero_send"
RECOVERY_DELIVERY_EVIDENCE_CONFIRMED = "confirmed"

_AUTHORIZATION_RE = re.compile(
    r"\[mozyo:recovery-delivery-authorization:(?P<body>[^\]]*)\]"
)
_ZERO_SEND_RE = re.compile(
    r"\[mozyo:recovery-delivery-zero-send:(?P<body>[^\]]*)\]"
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
    }
)
_ZERO_SEND_FIELDS = frozenset(
    {
        "conclusion",
        "issue",
        "lane",
        "workspace_id",
        "anchor_journal",
        "retry_of_action_sha256",
        "target_assigned_name_sha256",
        "outcome",
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
#: The caller's action-time authority (e.g. the lane's checkout binding) was no longer current
#: at the LAST point before transport — after target resolution and the delivery preflight
#: (Redmine #14475, review j#88538 F1). A zero-send: no injection was attempted.
DETAIL_AUTHORITY_MOVED = "authority_moved"
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
        DETAIL_AUTHORITY_MOVED,
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
        )


@dataclass(frozen=True)
class RecoveryDeliveryZeroSendEvidence:
    """Strict prior-outcome fact read from the exact evidence journal."""

    journal: str
    conclusion: str
    issue: str
    lane: str
    workspace_id: str
    anchor_journal: str
    retry_of_action_sha256: str
    target_assigned_name_sha256: str
    outcome: str
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
        evidence_journal: object,
        anchor_journal: object,
        retry_of_action_id: object,
        target_assigned_name: object,
    ) -> bool:
        return bool(
            self.journal == _norm(evidence_journal)
            and self.conclusion == RECOVERY_DELIVERY_EVIDENCE_CONFIRMED
            and self.issue == _norm(issue)
            and self.lane == _norm(lane)
            and self.workspace_id == _norm(workspace_id)
            and self.anchor_journal == _norm(anchor_journal)
            and self.retry_of_action_sha256
            == hashlib.sha256(_norm(retry_of_action_id).encode("utf-8")).hexdigest()
            and self.target_assigned_name_sha256
            == hashlib.sha256(_norm(target_assigned_name).encode("utf-8")).hexdigest()
            and self.outcome == RECOVERY_DELIVERY_PRIOR_ZERO_SEND
            and self.typed_count == "0"
            and self.send_count == "0"
            and self.turn_start_count == "0"
            and self.target_count == "0"
        )


def _strict_marker_fields(
    notes: str,
    *,
    pattern: re.Pattern[str],
    expected: frozenset[str],
) -> dict[str, str] | None:
    """Exactly one marker of ``pattern``, read by the SHARED strict grammar (pure).

    This module owns which channel it reads and how many markers a journal may carry; it does not
    own what a renderable body looks like (Redmine #14539 review j#92174 finding 3). The body split
    that used to live here was strict, but privately so, which is how a grammar drifts unobserved.

    Routing it to :func:`strict_marker_body_fields` also tightens it: the old loop stripped each
    component before judging it, so ``issue = 14539`` passed as a clean ``issue`` field even though
    no canonical producer renders it. The closed-field-set and repeated-key rules are unchanged.
    """
    if not isinstance(notes, str):
        return None
    matches = tuple(pattern.finditer(notes))
    if len(matches) != 1:
        return None
    return strict_marker_body_fields(matches[0].group("body"), expected=expected)


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
        if not journal:
            continue
        fields = _strict_marker_fields(
            notes, pattern=_AUTHORIZATION_RE, expected=_AUTHORIZATION_FIELDS
        )
        if fields is None:
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
            )
        )
    return tuple(found)


def parse_recovery_delivery_zero_send_evidence(
    entries: Iterable[object],
) -> tuple[RecoveryDeliveryZeroSendEvidence, ...]:
    """Parse strict zero-send facts from the exact prior evidence journal."""

    found: list[RecoveryDeliveryZeroSendEvidence] = []
    for entry in entries:
        journal = _norm(getattr(entry, "journal_id", ""))
        notes = getattr(entry, "notes", "")
        if not journal:
            continue
        fields = _strict_marker_fields(
            notes, pattern=_ZERO_SEND_RE, expected=_ZERO_SEND_FIELDS
        )
        if fields is None:
            continue
        found.append(
            RecoveryDeliveryZeroSendEvidence(
                journal=journal,
                conclusion=fields["conclusion"],
                issue=fields["issue"],
                lane=fields["lane"],
                workspace_id=fields["workspace_id"],
                anchor_journal=fields["anchor_journal"],
                retry_of_action_sha256=fields["retry_of_action_sha256"],
                target_assigned_name_sha256=fields["target_assigned_name_sha256"],
                outcome=fields["outcome"],
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
    )
    if any(not value for _key, value in fields):
        raise ValueError("recovery delivery authorization requires every exact authority field")
    return (
        f"[mozyo:{RECOVERY_DELIVERY_AUTHORIZATION_CHANNEL}:"
        + ":".join(f"{key}={value}" for key, value in fields)
        + "]"
    )


def build_recovery_delivery_zero_send_marker(
    *,
    issue: object,
    lane: object,
    workspace_id: object,
    anchor_journal: object,
    retry_of_action_id: object,
    target_assigned_name: object,
) -> str:
    """Build the strict prior-outcome marker consumed by the live authority read."""

    fields = (
        ("conclusion", RECOVERY_DELIVERY_EVIDENCE_CONFIRMED),
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
        (
            "target_assigned_name_sha256",
            hashlib.sha256(_norm(target_assigned_name).encode("utf-8")).hexdigest()
            if _norm(target_assigned_name)
            else "",
        ),
        ("outcome", RECOVERY_DELIVERY_PRIOR_ZERO_SEND),
        ("typed_count", "0"),
        ("send_count", "0"),
        ("turn_start_count", "0"),
        ("target_count", "0"),
    )
    if any(not value for _key, value in fields):
        raise ValueError("recovery zero-send evidence requires every exact outcome field")
    return (
        f"[mozyo:{RECOVERY_DELIVERY_ZERO_SEND_CHANNEL}:"
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
    "RECOVERY_DELIVERY_ZERO_SEND_CHANNEL",
    "RECOVERY_DELIVERY_AUTHORIZED",
    "RECOVERY_DELIVERY_AUTHORIZER_OWNER",
    "RECOVERY_DELIVERY_PRIOR_ZERO_SEND",
    "RECOVERY_DELIVERY_EVIDENCE_CONFIRMED",
    "RecoveryDeliveryAuthorization",
    "RecoveryDeliveryZeroSendEvidence",
    "RecoveryAnchorDeliveryPreflight",
    "build_recovery_delivery_authorization_marker",
    "build_recovery_delivery_zero_send_marker",
    "parse_recovery_delivery_authorizations",
    "parse_recovery_delivery_zero_send_evidence",
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
    "DETAIL_AUTHORITY_MOVED",
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
