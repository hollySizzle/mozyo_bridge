"""`herdr startup-status` — read-only startup evidence for ONE action (Redmine #14231).

The gap this closes (Design Consultation Answer j#84724, public-surface section): when a
managed launch's provider vanished, the only public signal was ``provider_exited`` — a
liveness verdict derived purely from "the locator's row is not in the live inventory",
which keeps no exit code, no stderr, and no stage. Everything that WOULD say where the
launch actually stopped lives in the action-scoped
:mod:`...core.state.startup_execution_events` projection, and nothing public reads it.

This is that read. It is deliberately its OWN command rather than a new ``doctor`` section
(the j#84722 correction): ``doctor``'s herdr section reports on the LIVE inventory and its
counts are a byte-invariant contract, so folding a vanished generation's evidence into it
would break what that section means. A vanished action is exactly the case ``doctor``
cannot describe, so it gets a surface whose subject is the action, not the inventory.

Authority boundary (j#84724): this is **diagnostic only**. The projection it reads is a
non-authority append-only record; it never promotes to rollback / close / adopt / approval
authority, and this command performs no mutation of any kind. Missing evidence is reported
as missing (:data:`...startup_execution_events.REASON_STARTUP_EVIDENCE_UNAVAILABLE`), never
silently strengthened into "the wrapper never ran" or "the provider exited".

Value safety: every field emitted here is an id, an assigned name, or a closed vocabulary
token from the projection. No path, env value, pane body, stderr text, or credential is
read or printed — the projection has no field carrying any of those (the wrapper only ever
appends bounded tokens), so this surface inherits that guarantee rather than filtering.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from mozyo_bridge.core.state.startup_execution_events import (
    JOIN_INVENTORY_UNREADABLE,
    JOIN_NOT_APPLICABLE,
    JOIN_POST_EXEC_LOCATOR_ABSENT,
    JOIN_PROVIDER_LIVE_CONFIRMED,
    REASON_STARTUP_EVIDENCE_UNAVAILABLE,
    STAGE_NO_EVIDENCE,
    classify_startup_evidence,
    read_execution_events,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    StartupTransactionError,
    StartupTransactionFence,
)

#: The action id names no row in this store. Distinct from "the action exists but has no
#: evidence": here we cannot even confirm the action was ever reserved here.
STATUS_ACTION_UNKNOWN = "action_unknown"
#: The startup-action authority itself could not be read (absent / damaged / unreadable).
#: Fail-closed: never reported as "no such action".
STATUS_AUTHORITY_UNAVAILABLE = "authority_unavailable"
#: The action was read and its evidence (present or absent) is reported.
STATUS_OK = "ok"

#: Per-participant next actions, keyed by the joined evidence verdict. Fixed operator
#: instructions — no value is ever interpolated in.
_NEXT_ACTION_BY_JOIN: dict[str, str] = {
    JOIN_PROVIDER_LIVE_CONFIRMED: (
        "the provider is live at the locator this action launched; no recovery is "
        "needed for this participant"
    ),
    JOIN_POST_EXEC_LOCATOR_ABSENT: (
        "the wrapper reached the exec call but the locator is no longer live; "
        "converge this action with `mozyo-bridge herdr session-rollback --action-id "
        "<id>` (read-only by default) before relaunching the slot"
    ),
    JOIN_INVENTORY_UNREADABLE: (
        "the live inventory could not be read, so liveness is unknown (NOT absent); "
        "re-run `mozyo-bridge doctor` once the herdr transport answers, then re-read "
        "this status"
    ),
    JOIN_NOT_APPLICABLE: (
        "the wrapper stopped before the provider exec call; read `last_stage` for the "
        "step it reached and `bounded_reason` for why — no liveness conclusion applies"
    ),
}

_NEXT_ACTION_NO_EVIDENCE = (
    "no execution-stage evidence exists for this action (an older launcher, a launch "
    "that predates this projection, or an evidence write that never landed). This is a "
    "reporting gap, NOT proof the wrapper never ran; classify from the action's own "
    "participants (`herdr session-rollback --action-id <id>`) instead"
)


@dataclass(frozen=True)
class ParticipantStartupStatus:
    """One participant's joined startup evidence (value-free, JSON-serializable)."""

    role: str
    assigned_name: str
    locator: str
    closed: bool
    last_stage: str
    inventory_join: str
    evidence_gap: bool
    bounded_reason: str
    next_action: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "closed": self.closed,
            "last_stage": self.last_stage,
            "inventory_join": self.inventory_join,
            "evidence_gap": self.evidence_gap,
            "bounded_reason": self.bounded_reason,
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class StartupStatusReport:
    """The full read-only report for one startup action."""

    action_id: str
    status: str
    reason: str = ""
    phase: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    participants: tuple[ParticipantStartupStatus, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "status": self.status,
            "reason": self.reason,
            "phase": self.phase,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "participants": [p.as_payload() for p in self.participants],
        }


def build_startup_status(
    *,
    action_id: str,
    fence: StartupTransactionFence,
    live_locators: Optional[Sequence[str]],
) -> StartupStatusReport:
    """Join one action's participants with its execution-stage evidence (pure-ish read).

    ``live_locators`` is the caller's live-inventory observation: a sequence of currently
    live locators, or ``None`` when the inventory could not be read. ``None`` is NOT an
    empty inventory — it produces :data:`JOIN_INVENTORY_UNREADABLE` rather than letting an
    unreadable read masquerade as "the locator is gone".

    Performs no mutation. An unreadable authority is reported as
    :data:`STATUS_AUTHORITY_UNAVAILABLE` (fail-closed), never as an unknown action.
    """
    normalized = (action_id or "").strip()
    try:
        action = fence.read(normalized)
    except StartupTransactionError as exc:
        return StartupStatusReport(
            action_id=normalized,
            status=STATUS_AUTHORITY_UNAVAILABLE,
            reason=str(exc),
        )
    if action is None:
        return StartupStatusReport(action_id=normalized, status=STATUS_ACTION_UNKNOWN)

    events = read_execution_events(fence, normalized)
    inventory_readable = live_locators is not None
    live = set(live_locators or ())

    participants = []
    for participant in action.participants:
        verdict = classify_startup_evidence(
            events,
            live_locator_observed=participant.locator in live,
            inventory_readable=inventory_readable,
        )
        if verdict.last_stage == STAGE_NO_EVIDENCE:
            next_action = _NEXT_ACTION_NO_EVIDENCE
        else:
            next_action = _NEXT_ACTION_BY_JOIN.get(verdict.inventory_join, "")
        participants.append(
            ParticipantStartupStatus(
                role=participant.role,
                assigned_name=participant.assigned_name,
                locator=participant.locator,
                closed=participant.closed,
                last_stage=verdict.last_stage,
                inventory_join=verdict.inventory_join,
                evidence_gap=verdict.evidence_gap,
                bounded_reason=verdict.bounded_reason,
                next_action=next_action,
            )
        )
    return StartupStatusReport(
        action_id=normalized,
        status=STATUS_OK,
        phase=action.phase,
        workspace_id=action.unit.workspace_id,
        lane_id=action.unit.lane_id,
        participants=tuple(participants),
    )


def _render_text(report: StartupStatusReport) -> str:
    lines = [f"herdr startup-status: action={report.action_id} status={report.status}"]
    if report.reason:
        lines.append(f"  reason: {report.reason}")
    if report.status != STATUS_OK:
        return "\n".join(lines)
    lines.append(
        f"  phase: {report.phase} workspace={report.workspace_id} lane={report.lane_id}"
    )
    if not report.participants:
        lines.append("  (this action recorded no participants — nothing was launched)")
        return "\n".join(lines)
    for participant in report.participants:
        line = (
            f"  - {participant.role}: stage={participant.last_stage} "
            f"join={participant.inventory_join} name={participant.assigned_name}"
        )
        if participant.locator:
            line += f" locator={participant.locator}"
        if participant.closed:
            line += " closed=yes"
        lines.append(line)
        if participant.bounded_reason:
            lines.append(f"      reason: {participant.bounded_reason}")
        if participant.evidence_gap:
            lines.append(
                f"      evidence gap: {REASON_STARTUP_EVIDENCE_UNAVAILABLE} "
                "(missing evidence is not proof of what happened)"
            )
        if participant.next_action:
            lines.append(f"      next: {participant.next_action}")
    return "\n".join(lines)


def _read_live_locators(repo_root, env) -> Optional[list[str]]:
    """Live locators from the shared herdr inventory read, or ``None`` when unreadable."""
    try:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
            read_herdr_inventory,
        )

        view = read_herdr_inventory(repo_root)
    except Exception:  # noqa: BLE001 — an unreadable inventory is a fact, not a crash
        return None
    if not getattr(view, "ok", False):
        return None
    return [
        agent.locator for agent in getattr(view, "agents", ()) if getattr(agent, "locator", "")
    ]


def cmd_herdr_startup_status(args: argparse.Namespace) -> int:
    """CLI entry: report one startup action's evidence. Read-only, never mutates."""
    import os

    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.shared.errors import die

    action_id = (getattr(args, "action_id", "") or "").strip()
    if not action_id:
        die(
            "herdr startup-status failed: --action-id is required. The status is scoped "
            "to one launch action; `herdr session-start` prints that id (and `--json` "
            "carries it as `action_id`)."
        )
        raise AssertionError("unreachable")
    repo_root = repo_root_from_args(args)
    report = build_startup_status(
        action_id=action_id,
        fence=StartupTransactionFence(),
        live_locators=_read_live_locators(repo_root, os.environ),
    )
    if getattr(args, "json", False):
        print(json.dumps(report.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(report))
    return 0 if report.ok else 1


def register_herdr_startup_status_parser(sub) -> None:
    """Bind `herdr startup-status` onto the `herdr` subparser group."""
    parser = sub.add_parser(
        "startup-status",
        help=(
            "read one startup action's typed launch evidence, including a generation "
            "that has already vanished from the live inventory (read-only)"
        ),
        description=(
            "Report where ONE session-start action's launch actually stopped, joining the "
            "action's participants with the append-only execution-stage evidence the "
            "wrapper recorded. Answers what `provider_exited` cannot: whether the wrapper "
            "was entered, whether the self-lookup resolved, whether the attestation was "
            "persisted, and whether the provider exec call was reached — and, only when "
            "the live inventory is readable, whether the locator is still live. Missing "
            "evidence is reported as missing, never as proof the wrapper never ran. "
            "Diagnostic only: it reads no authority, grants none, and mutates nothing. "
            "No path, env value, pane body, or stderr text is emitted."
        ),
    )
    parser.add_argument(
        "--action-id",
        required=True,
        help="the startup action id `herdr session-start` reported for the run",
    )
    parser.add_argument("--repo", help="target repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit the structured report")
    parser.set_defaults(func=cmd_herdr_startup_status)


__all__ = (
    "STATUS_ACTION_UNKNOWN",
    "STATUS_AUTHORITY_UNAVAILABLE",
    "STATUS_OK",
    "ParticipantStartupStatus",
    "StartupStatusReport",
    "build_startup_status",
    "cmd_herdr_startup_status",
    "register_herdr_startup_status_parser",
)
