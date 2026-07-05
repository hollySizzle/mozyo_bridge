"""Pure sublane worker-dispatch ack-drive vocabulary / outcome (Redmine #12988).

#12986 renamed the creation-side dispatch record honestly: a ``sublane create
--execute`` gateway ``handoff send`` exiting 0 is :data:`DISPATCH_GATEWAY_NOTIFIED`
only — the gateway pane received the notification, the same-lane Claude worker
start is unproven — and reserved :data:`DISPATCH_WORKER_DISPATCHED` for a future
real ack-driven step. #12988 delivers that step: the ``sublane dispatch-worker``
drive (application layer :mod:`...application.sublane_worker_dispatcher`) lets the
lane's Codex gateway forward the anchored ``implementation_request`` to its
same-lane worker over the governed ``handoff send --to claude`` rail and record
the *measured* outcome, so ``worker_dispatch_confirmed=true`` /
``worker_dispatched`` is only ever written from a real worker-transfer delivery
ACK — never inferred from gateway notification alone.

This module is the **pure vocabulary + outcome value object + durable-record
renderer** for that drive. It holds no IO and orchestrates nothing.

Semantics contract (``vibes/docs/logics/ack-completion-receiver-state.md``, the
ACK / delivery / completion separation): ``worker_dispatched`` is a **delivery
ACK at the worker pane** — the submit-complete rail handed the anchored
implementation_request to the worker runtime — and nothing more. It does NOT
claim the worker processed the request, made progress, or completed anything;
task completion stays with the durable Redmine record. The fail-closed side is
equally fixed: when the drive cannot confirm the transfer (missing identity /
anchor, unresolved lane, missing pane, failed / refused send) the lane's
recorded dispatch state **stays** :data:`DISPATCH_GATEWAY_NOTIFIED` — a failed
drive is :data:`WORKER_DISPATCH_DELIVERY_FAILED`, never a silent promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    DISPATCH_NOT_ATTEMPTED,
    DISPATCH_WORKER_DISPATCHED,
    REASON_ANCHOR_REQUIRED,
    REASON_LANE_MISMATCH,
    REASON_MISSING_IDENTITY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    SublaneLaneView,
    parse_issue_from_lane_label,
)

# ---------------------------------------------------------------------------
# Drive-specific result / blocked-reason tokens.
#
# The drive's own result is one of three literals: the worker transfer was
# confirmed (`worker_dispatched`, reusing the #12986 reserved token), the send
# was attempted and did not ack (`delivery_failed`, aligned with the
# transport-agnostic ACK contract state of the same name), or the drive never
# reached the send (`not_attempted`: dry-run, or blocked before dispatch).
# ---------------------------------------------------------------------------

#: The same-lane send was attempted but no delivery ACK landed (non-zero exit,
#: refused preflight, or a raised failure). The lane's recorded dispatch state
#: stays ``gateway_notified`` — fail-closed, never promoted.
WORKER_DISPATCH_DELIVERY_FAILED = "delivery_failed"

WORKER_DISPATCH_RESULTS = frozenset(
    {
        DISPATCH_WORKER_DISPATCHED,
        WORKER_DISPATCH_DELIVERY_FAILED,
        DISPATCH_NOT_ATTEMPTED,
    }
)

#: No lane resolved for the requested worktree in the live pane inventory.
REASON_LANE_NOT_RESOLVED = "lane_not_resolved"
#: The resolved lane has no live worker (or gateway) pane to transfer to /
#: call back to; dispatching would be unanchored.
REASON_LANE_PANE_MISSING = "lane_pane_missing"
#: The same-lane worker send returned a non-zero / failed outcome.
REASON_WORKER_DISPATCH_FAILED = "worker_dispatch_failed"

WORKER_DISPATCH_BLOCKED_REASONS = frozenset(
    {
        REASON_MISSING_IDENTITY,
        REASON_ANCHOR_REQUIRED,
        REASON_LANE_NOT_RESOLVED,
        REASON_LANE_MISMATCH,
        REASON_LANE_PANE_MISSING,
        REASON_WORKER_DISPATCH_FAILED,
    }
)


# ---------------------------------------------------------------------------
# Request / identity guard.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerDispatchRequest:
    """The identity + anchor the gateway supplies to drive the worker transfer.

    ``worktree_path`` names the lane worktree the drive resolves the live lane
    from (normally the repo root the gateway runs in). ``journal`` is the
    durable-anchor journal id the forwarded ``implementation_request`` carries —
    required for a live send, exactly like the #12973 actuator's dispatch step.
    """

    issue: str
    lane_label: str
    worktree_path: str
    journal: Optional[str] = None

    def missing_fields(self) -> Tuple[str, ...]:
        missing = []
        if not (self.issue or "").strip():
            missing.append("issue")
        if not (self.lane_label or "").strip():
            missing.append("lane_label")
        if not (self.worktree_path or "").strip():
            missing.append("worktree_path")
        return tuple(missing)


def lane_identity_matches(
    lane: SublaneLaneView, *, issue: str, lane_label: str
) -> bool:
    """True iff the resolved ``lane`` is the requested dispatch target (pure).

    The same guard the #12973 actuator applies before adopting / dispatching to
    a lane (Review j#70250): the lane's ``lane_label`` must equal the requested
    label and its issue must match the requested issue, re-parsing the label via
    :func:`parse_issue_from_lane_label` when the lane's ``issue`` field was not
    pre-populated. A blank requested label, a mismatched label, or a mismatched
    issue all fail closed — a repo-root / basename collision or a stale lane
    must never receive #<issue>'s implementation_request.
    """
    want_label = (lane_label or "").strip()
    got_label = (lane.lane_label or "").strip()
    if not want_label or got_label != want_label:
        return False
    want_issue = (issue or "").strip()
    got_issue = (lane.issue or "").strip() or (
        parse_issue_from_lane_label(got_label) or ""
    )
    if want_issue and got_issue != want_issue:
        return False
    return True


# ---------------------------------------------------------------------------
# Outcome value object.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerDispatchOutcome:
    """The machine-readable result of one ``sublane dispatch-worker`` drive.

    ``status`` reuses the actuation status vocabulary (:data:`ACTUATE_EXECUTED`
    / :data:`ACTUATE_READY` / :data:`ACTUATE_BLOCKED`); ``dispatch_result`` is
    one of :data:`WORKER_DISPATCH_RESULTS`. ``command`` is the replayable
    same-lane ``handoff send`` invocation (preview on a dry-run, retry material
    on a failure). ``gateway_pane`` doubles as the worker's recorded same-lane
    callback target.
    """

    status: str
    execute: bool
    reason: str
    issue: str
    lane_label: str
    worktree_path: Optional[str] = None
    gateway_pane: Optional[str] = None
    worker_pane: Optional[str] = None
    dispatch_target: Optional[str] = None
    dispatch_result: str = DISPATCH_NOT_ATTEMPTED
    durable_anchor: Optional[str] = None
    command: Optional[str] = None
    blocked_reasons: Tuple[str, ...] = ()
    # #13290 dispatch admission gate: the concrete FILL_* token the caller-supplied
    # fill decision resolved to (``None`` when the gate was not armed), and the
    # explicit override reason recorded when a stop was intentionally proceeded past.
    fill_decision: Optional[str] = None
    fill_override_reason: Optional[str] = None

    @property
    def is_blocked(self) -> bool:
        return self.status == ACTUATE_BLOCKED

    @property
    def executed(self) -> bool:
        return self.status == ACTUATE_EXECUTED

    @property
    def worker_dispatch_confirmed(self) -> bool:
        """True only when the worker-transfer delivery ACK was measured.

        Mirrors :attr:`SublaneActuationOutcome.worker_dispatch_confirmed`: only
        the explicit :data:`DISPATCH_WORKER_DISPATCHED` result confirms; a
        dry-run, a pre-send block, and a ``delivery_failed`` send all stay
        unconfirmed (Redmine #12986 / #12988).
        """
        return self.dispatch_result == DISPATCH_WORKER_DISPATCHED

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "execute": self.execute,
            "reason": self.reason,
            "issue": self.issue,
            "lane_label": self.lane_label,
            "worktree_path": self.worktree_path,
            "gateway_pane": self.gateway_pane,
            "worker_pane": self.worker_pane,
            "dispatch_target": self.dispatch_target,
            "dispatch_result": self.dispatch_result,
            "worker_dispatch_confirmed": self.worker_dispatch_confirmed,
            "durable_anchor": self.durable_anchor,
            "command": self.command,
            "blocked_reasons": list(self.blocked_reasons),
            "fill_decision": self.fill_decision,
            "fill_override_reason": self.fill_override_reason,
        }


def render_worker_dispatch_journal(outcome: WorkerDispatchOutcome) -> str:
    """Render the drive outcome as a replayable durable-record snippet (pure).

    The gateway posts this to the Redmine durable anchor so the coordinator can
    read the *measured* worker-dispatch state instead of inferring it from the
    creation-side ``gateway_notified`` record. A fail-closed run spells out that
    the lane's recorded dispatch state stays ``gateway_notified`` and carries
    the replayable retry command.
    """
    heading = (
        "## sublane worker dispatch blocked"
        if outcome.is_blocked
        else (
            "## sublane worker dispatched"
            if outcome.execute
            else "## sublane worker dispatch plan (dry-run)"
        )
    )
    lines = [
        heading,
        "",
        f"- issue: #{outcome.issue}",
        f"- lane_label: {outcome.lane_label or '-'}",
        f"- state: {outcome.status}",
        f"- execute: {str(outcome.execute).lower()}",
        f"- gateway_pane: {outcome.gateway_pane or '-'}",
        f"- worker_pane: {outcome.worker_pane or '-'}",
        f"- dispatch_target: {outcome.dispatch_target or '-'}",
        f"- dispatch_result: {outcome.dispatch_result}",
        f"- worker_dispatch_confirmed: {str(outcome.worker_dispatch_confirmed).lower()}",
        f"- durable_anchor: {outcome.durable_anchor or '-'}",
    ]
    # #13290: record the consulted fill decision and any explicit override so the
    # durable record carries the admission decision (reason + anchor) that let a
    # stop-classified worker dispatch proceed. Emitted only when the gate was armed.
    if outcome.fill_decision is not None:
        lines.append(f"- fill_decision: {outcome.fill_decision}")
    if outcome.fill_override_reason is not None:
        lines.append(f"- fill_stop_override: {outcome.fill_override_reason}")
    if outcome.command:
        lines.append(f"- command: `{outcome.command}`")
    if outcome.is_blocked:
        lines.append("- blocked_reasons: " + ", ".join(outcome.blocked_reasons))
    lines.append("- next_action: " + _next_action(outcome))
    return "\n".join(lines)


def _next_action(outcome: WorkerDispatchOutcome) -> str:
    """Honest next-action line for the drive outcome (pure).

    A confirmed transfer is still only a delivery ACK — the record must not read
    as worker progress or completion (ack-completion-receiver-state doctrine); a
    failed / blocked drive must keep the fail-closed ``gateway_notified``
    semantics explicit and recoverable instead of implying a started worker.
    """
    if not outcome.execute and not outcome.is_blocked:
        return "re-run with --execute to drive the same-lane worker transfer"
    if outcome.dispatch_result == DISPATCH_WORKER_DISPATCHED:
        return (
            "worker transfer delivery-acked (delivery ACK only — not worker "
            "progress or completion). Await the worker's durable journals and "
            "route callbacks per the coordinator-callback checklist"
        )
    return (
        "worker dispatch NOT confirmed; the lane's recorded dispatch state "
        "stays `gateway_notified` (fail-closed). Retry with the replayable "
        "command after fixing the blocked reason, or classify the stall with "
        "`mozyo-bridge sublane callback-recovery --dispatch-delivered`"
    )


__all__ = (
    "WORKER_DISPATCH_DELIVERY_FAILED",
    "WORKER_DISPATCH_RESULTS",
    "REASON_LANE_NOT_RESOLVED",
    "REASON_LANE_PANE_MISSING",
    "REASON_WORKER_DISPATCH_FAILED",
    "WORKER_DISPATCH_BLOCKED_REASONS",
    "WorkerDispatchRequest",
    "lane_identity_matches",
    "WorkerDispatchOutcome",
    "render_worker_dispatch_journal",
)
