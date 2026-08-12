"""Public result and participant records for the Herdr startup rollback rail."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: Refusals that are about the action, not about any one participant.
REASON_OK = "ok"
REASON_ACTION_UNKNOWN = "action_unknown"
REASON_AUTHORITY_UNAVAILABLE = "rollback_authority_unavailable"
REASON_NOTHING_OWED = "nothing_owed"
REASON_ALREADY_ROLLED_BACK = "already_rolled_back"
REASON_BUSY = "rollback_busy"
REASON_BLOCKED = "rollback_blocked"
REASON_CONDITIONAL_CLOSE_UNAVAILABLE = "conditional_close_unavailable"
REASON_INCOMPLETE = "rollback_incomplete"
REASON_PREFLIGHT = "preflight_only"

#: Prepared-pane observation states. ``unreadable`` includes a missing positive
#: input-empty fact: Herdr 0.8 has no public input-buffer field, so an empty historical
#: read or a prompt-shaped screen is not accepted as proof.
PREPARED_PANE_PRESENT = "present"
PREPARED_PANE_ABSENT = "absent"
PREPARED_PANE_UNREADABLE = "unreadable"
ROLLBACK_PREPARED_PANE_UNVERIFIABLE = "prepared_pane_unverifiable"
ROLLBACK_PREPARED_RECEIPT_INVALID = "prepared_pane_receipt_invalid"
ROLLBACK_PREPARED_NATIVE_MISMATCH = "prepared_pane_native_identity_mismatch"
ROLLBACK_PREPARED_TERMINAL_MISMATCH = "prepared_pane_terminal_identity_mismatch"


@dataclass(frozen=True)
class PreparedPaneObservation:
    """Positive facts about a pane that exists before ``agent start``.

    ``input_empty`` is deliberately three-valued. Only literal ``True`` satisfies that
    content fence; ``None`` means the runtime exposes no authoritative input-state fact.
    """

    state: str
    locator: str = ""
    workspace_id: str = ""
    tab_id: str = ""
    terminal_id: str = field(default="", repr=False)
    terminal_reclaimed: Optional[bool] = None
    agent_absent: bool = False
    shell_only: bool = False
    input_empty: Optional[bool] = None
    detail: str = ""


@dataclass(frozen=True)
class StartupRollbackAgentTarget:
    """Exact v2 native/terminal generation one startup action may close."""

    role: str
    assigned_name: str
    locator: str
    native_name: str
    terminal_id: str = field(repr=False)


@dataclass(frozen=True)
class ParticipantVerdict:
    role: str
    assigned_name: str
    locator: str
    verdict: str
    detail: str = ""
    blocker_id: str = ""
    closed: bool = False
    close_detail: str = ""
    #: Internal execution mode only. The public payload remains byte-compatible.
    prepared_pane: bool = False

    def as_payload(self) -> dict:
        return {
            "role": self.role,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "verdict": self.verdict,
            "detail": self.detail,
            "blocker_id": self.blocker_id,
            "closed": self.closed,
            "close_detail": self.close_detail,
        }


@dataclass(frozen=True)
class SessionRollbackVerdict:
    action_id: str
    state: str
    reason: str
    detail: str = ""
    executed: bool = False
    participants: tuple[ParticipantVerdict, ...] = ()

    @property
    def ok(self) -> bool:
        return self.reason in (REASON_OK, REASON_ALREADY_ROLLED_BACK)

    def as_payload(self) -> dict:
        return {
            "action_id": self.action_id,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "executed": self.executed,
            "participants": [p.as_payload() for p in self.participants],
        }


__all__ = (
    "PREPARED_PANE_ABSENT",
    "PREPARED_PANE_PRESENT",
    "PREPARED_PANE_UNREADABLE",
    "ParticipantVerdict",
    "PreparedPaneObservation",
    "REASON_ACTION_UNKNOWN",
    "REASON_ALREADY_ROLLED_BACK",
    "REASON_AUTHORITY_UNAVAILABLE",
    "REASON_BLOCKED",
    "REASON_BUSY",
    "REASON_CONDITIONAL_CLOSE_UNAVAILABLE",
    "REASON_INCOMPLETE",
    "REASON_NOTHING_OWED",
    "REASON_OK",
    "REASON_PREFLIGHT",
    "ROLLBACK_PREPARED_NATIVE_MISMATCH",
    "ROLLBACK_PREPARED_PANE_UNVERIFIABLE",
    "ROLLBACK_PREPARED_RECEIPT_INVALID",
    "ROLLBACK_PREPARED_TERMINAL_MISMATCH",
    "SessionRollbackVerdict",
    "StartupRollbackAgentTarget",
)
