"""The identity a vanished-gateway recovery is, before anything is done about it (#14741).

A gateway that vanished mid-turn is today healed directly and the dispatch retried. That
makes the heal's identity implicit -- two runs from the same durable request are "the same"
only because they happen to look alike -- and it never asks whether the launch it is
replacing owed an identity receipt. This module fixes the first half: what a recovery IS.

The deterministic action id is the point. A replacement transaction is keyed by
``(workspace, action_id)``, so the id is what makes a retry the SAME action rather than a
second one. It is therefore derived ONLY from things that are true of the request and the
participant, hashed over a canonical encoding:

* a schema tag, so a future change to what identity means cannot silently collide with an
  id minted under today's rules;
* the ORIGINAL implementation-request anchor -- the durable record that asked for the work,
  not the heal's own journal;
* the exact participant authority, all twelve axes including the evidence triplet.

What is deliberately NOT in it: the current working directory, the worktree, any repo path,
the clock, and the FRESH locator. Each of those varies between two runs that are the same
action -- a retry from a different checkout, a second later, landing on a different pane --
and putting any of them in the id would mint a new transaction for a request that already
has one. The path-independence test states exactly that.

This module reads nothing and writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

#: Bumped when the MEANING of the identity inputs changes. Two ids minted under different
#: schemas are different actions even if every other input matches -- which is the honest
#: answer, because the older id was computed from a different question.
IDENTITY_SCHEMA = "vanished-gateway-recovery/1"

#: The one closed gate a vanished-gateway recovery resumes.
RESUME_GATE = "implementation_request"

#: The gateway's own redispatch action. NOT the worker's ``dispatch_once``: a gateway
#: re-delivers an implementation request to a lane it coordinates, and reusing the worker
#: token would make two different continuations indistinguishable in a stored row.
REDISPATCH_GATEWAY_ONCE = "redispatch_gateway_implementation_request_once"

#: The recovery is a plain direct heal: the vanished launch predates identity receipts, so
#: there is no receipt to prove and no transaction to write.
OUTCOME_LEGACY_DIRECT = "legacy_direct"
#: The fresh path confirmed an exact durable row is ready AFTER its own plan call. This is
#: an observation, not a claim of authorship: the transaction store answers ``applied=True``
#: for a pristine re-plan as well, and a deterministic id makes a peer's row byte-identical
#: to the one this run would have written, so no seam here can say who inserted it
#: (ruling j#97162).
OUTCOME_RECEIPT_PLANNED = "receipt_planned"
#: An exact row was confirmed BEFORE any plan call -- either it already existed on the fresh
#: path, or an explicit action-id replay found it. Also purely observational.
#:
#: The two outcomes are one terminal family for anything downstream: they differ in when the
#: row was observed, not in what authority it carries.
OUTCOME_REPLAYED = "replayed"

#: No launch-generation row for this participant: nobody recorded which action it belongs to.
REFUSE_GENERATION_UNAVAILABLE = "generation_unavailable"
#: A row exists but is not this participant, or has not attested.
REFUSE_GENERATION_MISMATCH = "generation_mismatch"
#: The action id matches no shape this build classifies. Never read as legacy (j#96892).
REFUSE_UNKNOWN_ACTION_SHAPE = "unknown_action_shape"
#: The request itself is not a set of exact tokens.
REFUSE_REQUEST_INVALID = "request_invalid"
#: A receipt-capable participant whose evidence could not be planned.
REFUSE_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
#: A stored row exists at this key but is not this action.
REFUSE_FOREIGN_TRANSACTION = "foreign_transaction"


#: The exact shape of a recovery action id. Validated BEFORE any store is opened, so an
#: unknown id is refused without reading the world (audit j#97151 R1).
ACTION_ID_RE = re.compile(r"recover-gateway:[0-9a-f]{64}")

#: This rail is Redmine-governed; an anchor from anywhere else is not an anchor here.
ANCHOR_SOURCE = "redmine"
_ASCII_DIGITS = frozenset("0123456789")
_MAX_ID_DIGITS = 18


def is_recovery_action_id(value: object) -> bool:
    """Is this exactly a recovery action id? (pure, total)"""
    return type(value) is str and bool(ACTION_ID_RE.fullmatch(value))


class VanishedGatewayRecoveryError(ValueError):
    """The request or the authority is not exact. Never raised for a refusal outcome."""


def _exact(value: object) -> str:
    """The token exactly as given, or ``""``. No strip, no coercion (j#97074)."""
    if type(value) is not str:
        return ""
    if not value or value != value.strip():
        return ""
    return value


@dataclass(frozen=True)
class RequestAnchor:
    """The ORIGINAL implementation request this recovery exists to re-deliver.

    Not the heal's own journal: a retry writes new journals, and if the anchor moved with
    them every retry would be a new action. The gate is carried because a durable record is
    only an anchor for the gate it actually posted.
    """

    source: str
    issue_id: str
    journal_id: str
    gate: str = RESUME_GATE

    def __post_init__(self) -> None:
        for name in ("source", "issue_id", "journal_id", "gate"):
            if not _exact(getattr(self, name)):
                raise VanishedGatewayRecoveryError(
                    "a recovery anchor requires exact non-empty "
                    "(source, issue_id, journal_id, gate)"
                )
        # Pinned HERE rather than left to the pointer constructors downstream (audit
        # j#97151 R4): an anchor that only fails when someone tries to build a pointer out
        # of it has already been used to compute an action id by then.
        if self.source != ANCHOR_SOURCE:
            raise VanishedGatewayRecoveryError(
                "this recovery rail is redmine-governed; another source is not an anchor"
            )
        for name in ("issue_id", "journal_id"):
            value = getattr(self, name)
            if (
                not value
                or len(value) > _MAX_ID_DIGITS
                or set(value) - _ASCII_DIGITS
                or value.lstrip("0") == ""
            ):
                raise VanishedGatewayRecoveryError(
                    f"anchor {name} must be a positive ASCII decimal id"
                )
        if self.gate != RESUME_GATE:
            raise VanishedGatewayRecoveryError(
                "a vanished-gateway recovery resumes the implementation_request gate only"
            )

    def as_identity(self) -> dict:
        return {
            "source": self.source,
            "issue_id": self.issue_id,
            "journal_id": self.journal_id,
            "gate": self.gate,
        }


@dataclass(frozen=True)
class ParticipantAuthority:
    """Every axis of the gateway this recovery replaces, exactly as stored.

    ``role`` is fixed to ``gateway`` and ``is_self`` to false: a vanished GATEWAY recovery
    that could name a worker, or itself, would be a different operation wearing this one's
    identity.
    """

    workspace_id: str
    lane_id: str
    provider: str
    assigned_name: str
    old_locator: str
    lane_revision: str
    lane_generation: str
    evidence_workspace_id: str = ""
    evidence_startup_action_id: str = ""
    evidence_cause: str = ""
    role: str = "gateway"
    is_self: bool = False

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "lane_id",
            "provider",
            "assigned_name",
            "old_locator",
            "lane_revision",
            "lane_generation",
            "role",
        ):
            if not _exact(getattr(self, name)):
                raise VanishedGatewayRecoveryError(
                    f"participant authority axis {name} must be an exact non-empty token"
                )
        for name in (
            "evidence_workspace_id",
            "evidence_startup_action_id",
            "evidence_cause",
        ):
            value = getattr(self, name)
            if type(value) is not str or value != value.strip():
                raise VanishedGatewayRecoveryError(
                    f"evidence axis {name} must be plain exact text or empty"
                )
        if self.role != "gateway":
            raise VanishedGatewayRecoveryError("this recovery replaces a gateway only")
        if self.is_self is not False:
            raise VanishedGatewayRecoveryError(
                "a vanished gateway is never the self participant"
            )
        triplet = (
            self.evidence_workspace_id,
            self.evidence_startup_action_id,
            self.evidence_cause,
        )
        if any(triplet) and not all(triplet):
            raise VanishedGatewayRecoveryError(
                "the evidence triplet is wholly empty or wholly present"
            )

    @property
    def carries_evidence(self) -> bool:
        return bool(self.evidence_startup_action_id)

    def as_identity(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "role": self.role,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "old_locator": self.old_locator,
            "lane_revision": self.lane_revision,
            "lane_generation": self.lane_generation,
            "evidence_workspace_id": self.evidence_workspace_id,
            "evidence_startup_action_id": self.evidence_startup_action_id,
            "evidence_cause": self.evidence_cause,
            "is_self": self.is_self,
        }


def recovery_identity_payload(anchor: RequestAnchor, authority: ParticipantAuthority) -> str:
    """The canonical encoding the action id is the digest of. (pure)

    Compact separators and sorted keys, so the bytes are a function of the VALUES and not of
    dict ordering, indentation or the Python version that happened to serialise them.
    """
    return json.dumps(
        {
            "schema": IDENTITY_SCHEMA,
            "anchor": anchor.as_identity(),
            "participant": authority.as_identity(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def recovery_action_id(anchor: RequestAnchor, authority: ParticipantAuthority) -> str:
    """The deterministic replacement action id for this exact recovery. (pure)

    Same request + same participant -> same id -> the SAME durable transaction, whatever
    directory, clock or fresh pane the retry runs from.
    """
    payload = recovery_identity_payload(anchor, authority)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"recover-gateway:{digest}"


def recovery_action_id_for_pin(anchor: "RequestAnchor", pin, *, workspace_id: str) -> str:
    """The action id of the FINAL manifest, computed from a planned or stored pin. (pure)

    Audit j#97151 R2: the id must be the identity of what the transaction actually holds.
    Deriving it from the caller's input and then letting the planner add the evidence made
    the id describe a participant the row does not contain -- and made a replay of that row
    uncomputable from the row itself.

    ``workspace_id`` is passed rather than read off the pin because a pin does not carry
    one: on a replay it is the key's workspace, which is the same value the row is filed
    under, so the recomputation is anchored to where the row actually lives.
    """
    authority = ParticipantAuthority(
        workspace_id=workspace_id,
        lane_id=getattr(pin, "lane_id", ""),
        provider=getattr(pin, "provider", ""),
        assigned_name=getattr(pin, "assigned_name", ""),
        old_locator=getattr(pin, "old_locator", ""),
        lane_revision=getattr(pin, "lane_revision", ""),
        lane_generation=getattr(pin, "lane_generation", ""),
        evidence_workspace_id=getattr(pin, "evidence_workspace_id", ""),
        evidence_startup_action_id=getattr(pin, "evidence_startup_action_id", ""),
        evidence_cause=getattr(pin, "evidence_cause", ""),
        role=getattr(pin, "role", ""),
        is_self=bool(getattr(pin, "is_self", False)),
    )
    return recovery_action_id(anchor, authority)


@dataclass(frozen=True)
class RecoveryDecision:
    """What this recovery is, once its authority has been classified. (pure value)"""

    outcome: str = ""
    refusal: str = ""
    action_id: str = ""
    detail: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.refusal)


def refuse(reason: str, detail: str = "") -> RecoveryDecision:
    return RecoveryDecision(refusal=reason, detail=detail)


__all__ = (
    "ACTION_ID_RE",
    "ANCHOR_SOURCE",
    "IDENTITY_SCHEMA",
    "OUTCOME_LEGACY_DIRECT",
    "OUTCOME_RECEIPT_PLANNED",
    "OUTCOME_REPLAYED",
    "REDISPATCH_GATEWAY_ONCE",
    "REFUSE_EVIDENCE_UNAVAILABLE",
    "REFUSE_FOREIGN_TRANSACTION",
    "REFUSE_GENERATION_MISMATCH",
    "REFUSE_GENERATION_UNAVAILABLE",
    "REFUSE_REQUEST_INVALID",
    "REFUSE_UNKNOWN_ACTION_SHAPE",
    "RESUME_GATE",
    "ParticipantAuthority",
    "RecoveryDecision",
    "RequestAnchor",
    "VanishedGatewayRecoveryError",
    "is_recovery_action_id",
    "recovery_action_id",
    "recovery_action_id_for_pin",
    "recovery_identity_payload",
    "refuse",
)
