"""Typed authority for the worker leg of a recovered managed pair (#14203 R18)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    DISPATCH_KIND_IMPLEMENTATION_REQUEST,
    MARKER_CHANNEL_HANDOFF,
    MARKER_CHANNEL_WORKFLOW_EVENT,
    strict_marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    recovery_delivery_action_id,
)


def _norm(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def recovered_worker_forward_attempt_id(
    *,
    issue: object,
    lane: object,
    workspace_id: object,
    lane_generation: object,
    lifecycle_decision_journal: object,
    anchor_journal: object,
    target_action_id: object,
    target_assigned_name: object,
) -> str:
    """Identify the proven-zero standard forward that recovery may replace.

    The lifecycle decision (why this generation is active) and the work anchor
    (what the worker receives) remain distinct fields.  The digest binds both,
    the exact worker generation, and the startup action without exposing those
    values in the identifier.
    """

    fields = {
        "issue": _norm(issue),
        "lane": _norm(lane),
        "workspace_id": _norm(workspace_id),
        "lane_generation": _norm(lane_generation),
        "lifecycle_decision_journal": _norm(lifecycle_decision_journal),
        "anchor_journal": _norm(anchor_journal),
        "target_action_id": _norm(target_action_id),
        "target_assigned_name": _norm(target_assigned_name),
    }
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise ValueError(
            "a recovered worker-forward attempt id requires every authority field "
            f"(missing: {', '.join(missing)})"
        )
    encoded = json.dumps(
        fields,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "worker-forward-zero-send-" + hashlib.sha256(
        encoded.encode("utf-8")
    ).hexdigest()


def is_exact_implementation_request_anchor(
    entry: object,
    *,
    issue: object,
    journal: object,
    lane: object,
    lane_generation: object,
) -> bool:
    """Validate one fresh Redmine entry as the exact worker work anchor.

    Both canonical historical shapes are recognized: a workflow-event marker
    whose ``gate`` or ``kind`` is ``implementation_request``, and a handoff
    marker whose ``kind`` is ``implementation_request``. Optional identity
    fields must agree when present. Exactly one matching semantic marker is
    required, so duplicate or mixed markers fail closed.
    """

    issue_s = _norm(issue)
    journal_s = _norm(journal)
    lane_s = _norm(lane)
    generation_s = _norm(lane_generation)
    if not all((issue_s, journal_s, lane_s, generation_s)):
        return False
    if (
        _norm(getattr(entry, "issue_id", "")) != issue_s
        or _norm(getattr(entry, "journal_id", "")) != journal_s
    ):
        return False
    matches = 0
    # Effect-reaching: an exact work anchor gates target send readiness and redispatch, so an
    # unreadable marker ANYWHERE in the note refuses the anchor (review j#92060 finding 3). Note
    # this must refuse the whole note rather than skip the bad marker — skipping would make a note
    # carrying one clean and one forged marker read exactly like a clean one.
    scanned = strict_marker_fields_in_note(_norm(getattr(entry, "notes", "")))
    if scanned is None:
        return False
    for channel, fields in scanned:
        kind = _norm(fields.get("kind"))
        gate = _norm(fields.get("gate"))
        if channel == MARKER_CHANNEL_WORKFLOW_EVENT:
            semantics = {value for value in (kind, gate) if value}
            if DISPATCH_KIND_IMPLEMENTATION_REQUEST not in semantics:
                continue
            if semantics != {DISPATCH_KIND_IMPLEMENTATION_REQUEST}:
                return False
        elif channel == MARKER_CHANNEL_HANDOFF:
            if kind != DISPATCH_KIND_IMPLEMENTATION_REQUEST:
                continue
            if (
                _norm(fields.get("source")) != "redmine"
                or _norm(fields.get("issue")) != issue_s
                or _norm(fields.get("journal")) != journal_s
            ):
                return False
        else:
            continue
        if _norm(fields.get("issue")) not in ("", issue_s):
            return False
        if _norm(fields.get("journal")) not in ("", journal_s):
            return False
        if _norm(fields.get("lane")) not in ("", lane_s):
            return False
        if _norm(fields.get("lane_generation")) not in ("", generation_s):
            return False
        matches += 1
    return matches == 1


@dataclass(frozen=True)
class RecoveredWorkerDeliveryRequest:
    """One owner-approved direct recovery leg to an exact recovered worker."""

    issue: str
    lane: str
    journal: str
    implementation_request_journal: str
    lifecycle_decision_journal: str
    target_action_id: str
    retry_of_action_id: str
    prior_zero_send_journal: str

    def action_id(self) -> str:
        return recovery_delivery_action_id(
            issue=self.issue,
            lane=self.lane,
            approval_journal=self.journal,
            anchor_journal=self.implementation_request_journal,
            retry_of_action_id=self.retry_of_action_id,
            prior_zero_send_journal=self.prior_zero_send_journal,
        )


@dataclass(frozen=True)
class RecoveredWorkerDeliveryOutcome:
    executed: bool
    issue: str
    lane: str
    action_id: str = ""
    may_deliver: bool = False
    redispatch: str = "redispatch_not_reached"
    detail: str = ""

    @property
    def is_blocked(self) -> bool:
        if not self.may_deliver:
            return True
        if not self.executed:
            return False
        return self.redispatch not in ("redispatched", "already_redispatched")

    def as_payload(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "issue": self.issue,
            "lane": self.lane,
            "action_id": self.action_id,
            "may_deliver": self.may_deliver,
            "redispatch": self.redispatch,
            "is_blocked": self.is_blocked,
            "detail": self.detail,
        }


__all__ = (
    "RecoveredWorkerDeliveryOutcome",
    "RecoveredWorkerDeliveryRequest",
    "is_exact_implementation_request_anchor",
    "recovered_worker_forward_attempt_id",
)
