"""Pure worker-dispatch admission/result vocabulary (#12988, #13846).

``worker_dispatched`` requires current generation-bound authority, a transport
ACK, and a causally-bound worker turn-start.  It is still not progress or task
completion; those remain durable workflow facts.  Any missing/uncertain fact
keeps the lane at ``gateway_notified`` and forbids an automatic replay.
"""

from __future__ import annotations

import json
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
# The drive's own result records transport and turn-start separately: the worker
# transfer was
# confirmed (`worker_dispatched`, reusing the #12986 reserved token), the send
# was attempted and did not ack (`delivery_failed`, aligned with the
# transport-agnostic ACK contract state), transport ACK lacked turn-start
# (`turn_start_unconfirmed`), or the drive never reached the send (`not_attempted`).
# ---------------------------------------------------------------------------

#: The same-lane send was attempted but no delivery ACK landed (non-zero exit,
#: refused preflight, or a raised failure). The lane's recorded dispatch state
#: stays ``gateway_notified`` — fail-closed, never promoted.
WORKER_DISPATCH_DELIVERY_FAILED = "delivery_failed"
#: Queue entry was acknowledged, but no causally-bound worker turn-start was
#: observed.  Injection may have happened, so this is deliberately non-retryable.
WORKER_DISPATCH_TURN_START_UNCONFIRMED = "turn_start_unconfirmed"

WORKER_DISPATCH_RESULTS = frozenset(
    {
        DISPATCH_WORKER_DISPATCHED,
        WORKER_DISPATCH_DELIVERY_FAILED,
        WORKER_DISPATCH_TURN_START_UNCONFIRMED,
        DISPATCH_NOT_ATTEMPTED,
    }
)

#: The sender that drove ``dispatch-worker`` is not the target lane's current
#: same-lane gateway (Redmine #14192): a foreign / cross-lane sender (e.g. a
#: coordinator shell) whose launch-time lane identity does not resolve to this
#: lane's gateway route. The pre-reserve sender preflight fails closed on it with
#: zero fence write and zero send, so the inner rail's ``gateway_route_blocked``
#: never reserves-then-poisons the exact key. Same verdict on dry-run and execute.
REASON_FOREIGN_SENDER = "foreign_sender_route_blocked"
#: No lane resolved for the requested worktree in the live pane inventory.
REASON_LANE_NOT_RESOLVED = "lane_not_resolved"
#: The resolved lane has no live worker (or gateway) pane to transfer to /
#: call back to; dispatching would be unanchored.
REASON_LANE_PANE_MISSING = "lane_pane_missing"
#: The same-lane worker send returned a non-zero / failed outcome.
REASON_WORKER_DISPATCH_FAILED = "worker_dispatch_failed"
REASON_WORKER_TURN_START_UNCONFIRMED = "worker_turn_start_unconfirmed"

# Action-time liveness-authority admission (#13846).  A stale-name token is a
# diagnosis, never permission to close/relaunch or to inject another request.
ADMISSION_HEALTHY = "healthy"
ADMISSION_STALE_WORKER_RECOVERY_REQUIRED = "stale_worker_recovery_required"
ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT = "worker_liveness_authority_conflict"
WORKER_DISPATCH_ADMISSION_DECISIONS = frozenset(
    {
        ADMISSION_HEALTHY,
        ADMISSION_STALE_WORKER_RECOVERY_REQUIRED,
        ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,
    }
)

TURN_START_STARTED = "started"
TURN_START_DELIVERED_NOT_STARTED = "delivered_not_started"
TURN_START_NOT_STARTED = "not_started"
TURN_START_UNKNOWN = "unknown"

WORKER_DISPATCH_BLOCKED_REASONS = frozenset(
    {
        REASON_MISSING_IDENTITY,
        REASON_ANCHOR_REQUIRED,
        REASON_FOREIGN_SENDER,
        REASON_LANE_NOT_RESOLVED,
        REASON_LANE_MISMATCH,
        REASON_LANE_PANE_MISSING,
        REASON_WORKER_DISPATCH_FAILED,
        REASON_WORKER_TURN_START_UNCONFIRMED,
        ADMISSION_STALE_WORKER_RECOVERY_REQUIRED,
        ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,
    }
)

#: The inner ``handoff send`` outcome reasons that PROVE a known-not-sent before any
#: injection (Redmine #14192). The gateway-route enforcement gate — the sender-lane
#: bypass block AND the lifecycle-target invariant, both emitted as
#: ``gateway_route_blocked`` — and the newer-schema ``reader_upgrade_required`` gate
#: all ``die`` *before* the transport rail types a byte. A non-zero worker send whose
#: structured outcome carries one of these reasons is therefore a proven zero-injection
#: (text / Enter 0), NOT an uncertain send: the exact fence key is cancelled (never
#: replayed) rather than poisoned to the reconcile-only ``uncertain`` terminal. Every
#: other non-zero outcome — an unparseable record, a timeout, or any post-injection
#: failure whose fate is unknown — stays ``uncertain`` and never-replay (Acceptance #3).
SEND_KNOWN_NOT_SENT_REASONS = frozenset(
    {"gateway_route_blocked", "reader_upgrade_required"}
)


def classify_send_known_not_sent(record_text: str) -> bool:
    """True iff the captured inner-send record proves a pre-injection zero-send (pure).

    The composed inner ``handoff send`` emits its structured :class:`DeliveryOutcome`
    with the default ``record_format=both``, whose LAST line is the single-line
    ``outcome.to_json()`` (the documented scrape target). This reads that last JSON
    object and returns ``True`` only for an explicit ``status == "blocked"`` whose
    ``reason`` is in :data:`SEND_KNOWN_NOT_SENT_REASONS` — a gate that fails closed
    before injection. Anything else — no JSON line, a non-object payload, a
    non-``blocked`` status, an unrecognized reason, or a parse failure — returns
    ``False`` so an ambiguous / post-injection / uncertain outcome is never mistaken
    for a proven not-sent (fail toward ``uncertain``, Redmine #14192 Acceptance #3).
    """
    for line in reversed((record_text or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        return (
            str(payload.get("status", "")) == "blocked"
            and str(payload.get("reason", "")) in SEND_KNOWN_NOT_SENT_REASONS
        )
    return False


@dataclass(frozen=True)
class SenderDispatchAdmission:
    """The pre-reserve sender-identity verdict for a worker dispatch (Redmine #14192).

    ``admitted`` is the sole authorization: only an admitted sender proceeds to the
    outbox reserve + send. ``reason`` is a public-safe explanation (no locator /
    secret); ``detail_token`` is a value-free discriminator (e.g. the resolved
    :class:`GatewayRouteDecision` ``blocked_reason``, or a sender-identity failure
    reason) surfaced in the blocked outcome so a recurrence is diagnosable.
    ``exception_applied`` records an admitted cross-lane drive released only by the
    explicit ``--allow-direct-worker`` durable exception (mirrors the inner gateway
    route's ``gateway_route_exception``).
    """

    admitted: bool
    reason: str
    detail_token: str = ""
    exception_applied: bool = False


@dataclass(frozen=True)
class WorkerDispatchAdmissionFacts:
    """One action-time join of durable and live worker authority."""

    lifecycle_current: bool
    anchor_current: bool
    identity_attested: bool
    action_binding_current: bool
    slot_state: str
    locator_present: bool
    receiver_state: str
    generation_binding_current: bool = False
    # #13846 R4: a value-free token naming WHICH generation authority did not bind when
    # ``generation_binding_current`` is False (e.g. a slot-less fresh row whose live startup
    # self-attestation is not generation-bound, or a declared-pin identity / locator / revision
    # divergence). Empty when current. Surfaced in the conflict reason so a recurrence is
    # diagnosable from the public structured outcome without exposing a locator / secret.
    generation_binding_detail: str = ""
    terminal_absence_authoritative: bool = False
    duplicate_or_uncertain_delivery: bool = False
    workspace_id: Optional[str] = None
    lane_id: Optional[str] = None
    lane_generation: Optional[int] = None
    worker_assigned_name: Optional[str] = None
    worker_locator: Optional[str] = None
    action_id: Optional[str] = None


@dataclass(frozen=True)
class WorkerDispatchAdmission:
    """Typed dispatch decision; only ``healthy`` authorizes one injection."""

    decision: str
    reason: str
    facts: WorkerDispatchAdmissionFacts

    @property
    def is_healthy(self) -> bool:
        return self.decision == ADMISSION_HEALTHY

    @property
    def retry_allowed(self) -> bool:
        # A stale decision authorizes recovery, not replay of this injection.
        return False


def decide_worker_dispatch_admission(
    facts: WorkerDispatchAdmissionFacts,
) -> WorkerDispatchAdmission:
    """Fail closed over lifecycle, attestation, receiver and delivery causality."""
    authority_checks = (
        (facts.lifecycle_current, "lane lifecycle generation is not current"),
        (facts.anchor_current, "dispatch anchor is not the current lane decision"),
        (
            facts.generation_binding_current,
            "the live or absent worker is not bound to the current declared process generation"
            + (
                f" ({facts.generation_binding_detail})"
                if facts.generation_binding_detail
                else ""
            ),
        ),
        (facts.action_binding_current, "replacement/action binding is not current"),
        (
            not facts.duplicate_or_uncertain_delivery,
            "an earlier exact dispatch may already have injected this request",
        ),
    )
    for ok, reason in authority_checks:
        if not ok:
            return WorkerDispatchAdmission(
                ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT, reason, facts
            )
    if facts.terminal_absence_authoritative and not facts.locator_present:
        return WorkerDispatchAdmission(
            ADMISSION_STALE_WORKER_RECOVERY_REQUIRED,
            "the current generation's worker is authoritatively absent; route the "
            "owner-governed stale-worker recovery before dispatch",
            facts,
        )
    checks = (
        (facts.identity_attested, "worker startup identity/generation is not attested"),
        (facts.slot_state == "live", "named worker slot is not positively live"),
        (facts.locator_present, "worker locator is absent"),
        (
            facts.receiver_state in ("awaiting_input", "turn_ended"),
            "worker receiver is not presently dispatch-admissible",
        ),
    )
    for ok, reason in checks:
        if not ok:
            return WorkerDispatchAdmission(
                ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT, reason, facts
            )
    return WorkerDispatchAdmission(
        ADMISSION_HEALTHY,
        "current lifecycle, startup attestation, receiver and action binding agree",
        facts,
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
    # #13301 pre-dispatch worker readiness wait (bounded, non-fatal, mirroring the
    # #13293 gateway readiness wait). ``None`` when the wait was not run (dry-run,
    # disabled, or blocked before the send); ``True`` on a confirmed booted worker
    # pane; ``False`` when the window elapsed unconfirmed and the drive forwarded
    # anyway (the queue-enter rail never hard-blocks).
    worker_ready: Optional[bool] = None
    # #13301 route-gate integration: whether the explicit ``--allow-direct-worker``
    # durable exception (#12918) was threaded into the same-lane worker send so a
    # cross-lane drive (e.g. a coordinator stall-drive) is admitted and recorded
    # distinctly as a ``gateway_route_exception`` instead of failing closed.
    allow_direct_worker: bool = False
    # #13846 action-time authority and ACK/turn-start separation.  Additive JSON
    # fields keep existing consumers compatible while preventing ACK-only promotion.
    admission_decision: Optional[str] = None
    admission_reason: Optional[str] = None
    lane_generation: Optional[int] = None
    worker_assigned_name: Optional[str] = None
    receiver_state: Optional[str] = None
    turn_start_outcome: Optional[str] = None
    retry_allowed: bool = False

    @property
    def is_blocked(self) -> bool:
        return self.status == ACTUATE_BLOCKED

    @property
    def executed(self) -> bool:
        return self.status == ACTUATE_EXECUTED

    @property
    def worker_dispatch_confirmed(self) -> bool:
        """True only for healthy authority + delivery ACK + worker turn-start.

        A dry-run, pre-send block, delivery failure, or ACK-only result stays
        unconfirmed (#12986 / #12988 / #13846).
        """
        return (
            self.dispatch_result == DISPATCH_WORKER_DISPATCHED
            and self.admission_decision == ADMISSION_HEALTHY
            and self.turn_start_outcome == TURN_START_STARTED
        )

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
            "worker_ready": self.worker_ready,
            "allow_direct_worker": self.allow_direct_worker,
            "admission_decision": self.admission_decision,
            "admission_reason": self.admission_reason,
            "lane_generation": self.lane_generation,
            "worker_assigned_name": self.worker_assigned_name,
            "receiver_state": self.receiver_state,
            "turn_start_outcome": self.turn_start_outcome,
            "retry_allowed": self.retry_allowed,
        }


def render_worker_dispatch_journal(outcome: WorkerDispatchOutcome) -> str:
    """Render the drive outcome as a replayable durable-record snippet (pure).

    The gateway posts this to the Redmine durable anchor so the coordinator can
    read the measured state instead of inferring it from ``gateway_notified``.
    A fail-closed run keeps that state and says whether replay is forbidden.
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
    if outcome.admission_decision is not None:
        lines.extend(
            [
                f"- admission_decision: {outcome.admission_decision}",
                f"- admission_reason: {outcome.admission_reason or '-'}",
                f"- lane_generation: {outcome.lane_generation or '-'}",
                f"- worker_assigned_name: {outcome.worker_assigned_name or '-'}",
                f"- receiver_state: {outcome.receiver_state or '-'}",
            ]
        )
    if outcome.turn_start_outcome is not None:
        lines.append(f"- turn_start_outcome: {outcome.turn_start_outcome}")
    lines.append(f"- retry_allowed: {str(outcome.retry_allowed).lower()}")
    # #13290: record the consulted fill decision and any explicit override so the
    # durable record carries the admission decision (reason + anchor) that let a
    # stop-classified worker dispatch proceed. Emitted only when the gate was armed.
    if outcome.fill_decision is not None:
        lines.append(f"- fill_decision: {outcome.fill_decision}")
    if outcome.fill_override_reason is not None:
        lines.append(f"- fill_stop_override: {outcome.fill_override_reason}")
    # #13301: record the pre-dispatch worker readiness observation (only when the
    # wait ran) and the explicit route-gate exception so the durable record spells
    # out both hardening decisions — that the forward waited for a booted worker,
    # and that a cross-lane send was admitted distinctly, not silently.
    if outcome.worker_ready is not None:
        lines.append(f"- worker_ready: {str(outcome.worker_ready).lower()}")
    if outcome.allow_direct_worker:
        lines.append(
            "- route_exception: --allow-direct-worker "
            "(gateway_route_exception recorded, #12918)"
        )
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
            "worker transfer delivery-acked and turn-start observed (not worker "
            "progress or completion). Await the worker's durable journals and "
            "route callbacks per the coordinator-callback checklist"
        )
    if not outcome.retry_allowed:
        return (
            "worker dispatch NOT confirmed; fail-closed, the lane stays "
            "`gateway_notified`. "
            "Do not auto-retry because injection or receiver authority is uncertain. "
            "Re-observe the durable lane authority "
            "and route stale recovery through #13806 when required"
        )
    return (
        "worker dispatch NOT confirmed; the lane's recorded dispatch state "
        "stays `gateway_notified` (fail-closed). Retry with the replayable "
        "command after fixing the blocked reason, or classify the stall with "
        "`mozyo-bridge sublane callback-recovery --dispatch-delivered`"
    )


__all__ = (
    "WORKER_DISPATCH_DELIVERY_FAILED",
    "WORKER_DISPATCH_TURN_START_UNCONFIRMED",
    "WORKER_DISPATCH_RESULTS",
    "REASON_FOREIGN_SENDER",
    "SEND_KNOWN_NOT_SENT_REASONS",
    "classify_send_known_not_sent",
    "SenderDispatchAdmission",
    "REASON_LANE_NOT_RESOLVED",
    "REASON_LANE_PANE_MISSING",
    "REASON_WORKER_DISPATCH_FAILED",
    "REASON_WORKER_TURN_START_UNCONFIRMED",
    "WORKER_DISPATCH_BLOCKED_REASONS",
    "ADMISSION_HEALTHY",
    "ADMISSION_STALE_WORKER_RECOVERY_REQUIRED",
    "ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT",
    "WORKER_DISPATCH_ADMISSION_DECISIONS",
    "TURN_START_STARTED",
    "TURN_START_DELIVERED_NOT_STARTED",
    "TURN_START_NOT_STARTED",
    "TURN_START_UNKNOWN",
    "WorkerDispatchAdmissionFacts",
    "WorkerDispatchAdmission",
    "decide_worker_dispatch_admission",
    "WorkerDispatchRequest",
    "lane_identity_matches",
    "WorkerDispatchOutcome",
    "render_worker_dispatch_journal",
)
