"""Pure sublane live-actuation outcome / execution vocabulary (Redmine #12973).

#12955 delivered the ``mozyo-bridge sublane`` surface as *planning only*: ``create`` /
``start`` emit a fail-closed, replayable :class:`SublaneCreatePlan`, but never actuate the
``git worktree add`` / cockpit pane append / gateway dispatch a coordinator otherwise hand-
assembles for every max-5 sublane. #12973 adds the **creation-side live actuator** that
executes that plan (`sublane start --execute`), staying on the additive / boundary-approved
side of ``vibes/docs/logics/worktree-lifecycle-boundary.md`` (the #12604
:class:`LiveSublaneGitOperations.create_worktree` additive ``git worktree add`` is already
inside the boundary; the destructive retire-time merge / pane kill / worktree remove stays
gated and is untouched here).

This module is the **pure execution-state vocabulary + outcome value objects** for that
actuator. It holds no IO and orchestrates nothing: the application-layer use case
(:mod:`...application.sublane_actuator`) drives the injected port and assembles these VOs;
this module only names the machine-readable states and renders the durable-record snippet.

Three concerns, each pure:

- the per-step execution status vocabulary (:data:`STEP_EXECUTED` / :data:`STEP_READY` /
  :data:`STEP_SKIPPED` / :data:`STEP_BLOCKED`) and the overall actuation status
  (:data:`ACTUATE_EXECUTED` / :data:`ACTUATE_READY` / :data:`ACTUATE_BLOCKED`);
- the fail-closed blocked-reason tokens (:data:`REASON_*`) — a create-side actuator is
  *additive*, so its fail-closed set is missing identity, an unverified launch target, a
  missing durable anchor, a worktree collision (branch / path already taken), a pane-
  creation / stamp read-back failure, a **lane-identity mismatch** (the resolved lane's
  ``lane_label`` / ``issue`` does not match the request — a repo-root / basename collision
  or a stale lane, which would misdeliver to the wrong gateway), and a handoff-dispatch
  failure; a **dirty worktree fail-closed is a retire-side gate** (#12604
  :func:`decide_retire_integration`), because an additive create never clobbers an existing
  checkout — a collision surfaces as :data:`REASON_WORKTREE_CREATE_FAILED`, not silent data
  loss;
- the :class:`ActuationStep` / :class:`SublaneActuationOutcome` value objects and
  :func:`render_actuation_journal`, the replayable machine-readable record the coordinator
  posts to the Redmine durable anchor (the "Redmine durable record package" of the issue
  scope). Only issue id / lane label / state / launch action / gateway+worker pane / branch
  / worktree / dispatch target are emitted — runtime evidence the acceptance wants recorded
  (``sublane list --json`` shows the same lane), never a hidden ``%pane`` typed as normal UX.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_dispatch_admission import (
    REASON_FILL_STOP,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    portable_worktree_label,
)

# ---------------------------------------------------------------------------
# Per-step execution status (literal; machine-readable regardless of UI language).
# ---------------------------------------------------------------------------

#: The step ran and its side effect landed (live ``--execute`` run).
STEP_EXECUTED = "executed"
#: Dry-run: the step *would* run; no side effect was performed (the default UX).
STEP_READY = "ready"
#: The step was intentionally not performed (adopt an existing lane / pane, a non-Git skip,
#: or ``--no-dispatch``); not a failure.
STEP_SKIPPED = "skipped"
#: The step failed closed; actuation stopped here and no later step ran.
STEP_BLOCKED = "blocked"

STEP_STATES = frozenset({STEP_EXECUTED, STEP_READY, STEP_SKIPPED, STEP_BLOCKED})

# ---------------------------------------------------------------------------
# Overall actuation status.
# ---------------------------------------------------------------------------

#: A live ``--execute`` run completed every required step.
ACTUATE_EXECUTED = "executed"
#: A dry-run resolved a complete, replayable plan (would execute); no side effect.
ACTUATE_READY = "ready"
#: Fail-closed: a required identity / target / anchor was missing, or a step failed. No
#: partial success is reported as ok.
ACTUATE_BLOCKED = "blocked"

ACTUATE_STATES = frozenset({ACTUATE_EXECUTED, ACTUATE_READY, ACTUATE_BLOCKED})

# ---------------------------------------------------------------------------
# Fail-closed blocked-reason tokens.
# ---------------------------------------------------------------------------

#: A required sublane identity field (issue / lane_label / branch / worktree) was blank.
REASON_MISSING_IDENTITY = "missing_identity"
#: The pure #12604 launch decision refused (unverified target / branch not resolved).
REASON_LAUNCH_BLOCKED = "launch_blocked"
#: A live dispatch was requested but no durable-anchor journal id was supplied — the
#: workflow-step contract fails closed rather than dispatch a worker without an anchor.
REASON_ANCHOR_REQUIRED = "anchor_required"
#: ``git worktree add`` failed — the branch already exists, the worktree path is taken, or
#: git refused. Covers the acceptance's "branch collision" and "worktree collision".
REASON_WORKTREE_CREATE_FAILED = "worktree_create_failed"
#: The cockpit lane column could not be appended, or the read-back did not show a live
#: gateway + worker pane pair for the lane.
REASON_PANE_CREATE_FAILED = "pane_create_failed"
#: The appended / adopted lane did not carry the expected identity stamps (repo-root /
#: lane) on read-back, so the lane could not be positively confirmed.
REASON_STAMP_FAILED = "stamp_failed"
#: The lane resolved for the worktree does not match the requested lane identity
#: (lane_label / issue) — a repo-root / basename collision or a stale / different lane.
#: Adopting or dispatching to it would misdeliver #<issue> to the wrong gateway, so the
#: ambiguous target fails closed before any adopt / dispatch.
REASON_LANE_MISMATCH = "lane_identity_mismatch"
#: The gateway ``implementation_request`` dispatch returned a non-zero / failed outcome.
REASON_HANDOFF_FAILED = "handoff_failed"
#: The #13002 work-unit granularity gate refused: an ``epic`` / ``feature`` unit was
#: requested without an explicit owner / operator decision anchor (durable journal id).
REASON_WORK_UNIT_BLOCKED = "work_unit_blocked"
#: The action-time sender-attestation preflight refused a live dispatch (#13518 j#75671 / review
#: R2-F3): the coordinator sender identity was not attested, so actuating would create a lane /
#: worktree and THEN fail the governed dispatch on an unattested sender (a partial launch that
#: forces raw-input recovery). Fail closed BEFORE any worktree / launch side effect, with a
#: replayable resume plan (re-attest the sender, then re-run create).
REASON_SENDER_UNATTESTED = "sender_unattested"
#: The existing lane's gateway/worker pair is split across tabs / workspaces
#: (Redmine #13705): ``read_lane`` reports ``pair_split`` (both panes live but not
#: co-located). A same-tab-contract lane admits only ``active`` for adopt / dispatch,
#: so a split pair fails closed with zero append / dispatch — an actionable degraded
#: state recovered by retire + recreate, never adopted or healed over.
REASON_PAIR_SPLIT = "pair_split"
#: The action-time runtime fingerprint gate refused before any mutation (Redmine
#: #13705): the active runtime surface is missing placement behavior the repo-local
#: source ships (a source/installed skew — the exact class that split the pair). The
#: official mutating front door goes zero-write, so an incompatible / stale runtime
#: cannot actuate a lane it would place incorrectly.
REASON_RUNTIME_FINGERPRINT = "runtime_fingerprint"
#: The #13290 dispatch admission gate refused: the caller-supplied fill decision
#: resolved to a concrete stop and no explicit override was supplied. Defined in
#: :mod:`...domain.sublane_dispatch_admission`; re-exported here so the actuator's
#: fail-closed reason registry stays complete.
# (REASON_FILL_STOP imported above.)

BLOCKED_REASONS = frozenset(
    {
        REASON_MISSING_IDENTITY,
        REASON_LAUNCH_BLOCKED,
        REASON_ANCHOR_REQUIRED,
        REASON_WORKTREE_CREATE_FAILED,
        REASON_PANE_CREATE_FAILED,
        REASON_STAMP_FAILED,
        REASON_LANE_MISMATCH,
        REASON_HANDOFF_FAILED,
        REASON_WORK_UNIT_BLOCKED,
        REASON_SENDER_UNATTESTED,
        REASON_PAIR_SPLIT,
        REASON_RUNTIME_FINGERPRINT,
        REASON_FILL_STOP,
    }
)

# ---------------------------------------------------------------------------
# Dispatch outcome tokens recorded in the outcome / journal.
#
# Redmine #12986: the creation-side actuator used to record a single ``sent``
# token the moment the gateway ``handoff send`` exited 0, and the executed
# outcome / journal read that as a fully-started lane. But a gateway send exiting
# 0 proves only that the *gateway* pane received the notification — it does NOT
# prove the gateway forwarded the request to the same-lane Claude worker, nor
# that the worker started. #12982 / #12984 sat silently at ``sublane actuated``
# with no worker progress precisely because ``sent`` overstated that gateway
# notification as worker-start. The two states are now named distinctly so the
# durable record can never read a gateway-notified lane as worker-started.
# ---------------------------------------------------------------------------

#: The gateway ``handoff send`` exited 0: the gateway pane *received* the
#: implementation_request notification. This proves gateway notification only —
#: NOT that the same-lane worker was dispatched / started. A ``gateway_notified``
#: lane with no subsequent worker-dispatch ack is a ``no_progress_after_handoff``
#: candidate (classify with ``sublane callback-recovery --dispatch-delivered``).
DISPATCH_GATEWAY_NOTIFIED = "gateway_notified"
#: The same-lane worker actually received / acked the dispatched
#: implementation_request. The creation-side actuator does NOT reach this
#: state — it only notifies the gateway. The state is reached by the #12988
#: worker-dispatch ack drive (``mozyo-bridge sublane dispatch-worker
#: --execute``, :mod:`...application.sublane_worker_dispatcher`): the lane
#: gateway forwards the anchored request to its same-lane worker and only a
#: measured delivery ACK promotes the record to this token. It is a delivery
#: ACK, never worker progress / completion. The coordinator record and the
#: :attr:`SublaneActuationOutcome.worker_dispatch_confirmed` signal use it to
#: distinguish "gateway notified" from "worker dispatched".
DISPATCH_WORKER_DISPATCHED = "worker_dispatched"
#: Dispatch was intentionally skipped (``--no-dispatch``).
DISPATCH_SKIPPED = "skipped"
#: Dispatch was not reached (dry-run, or actuation blocked before the dispatch step).
DISPATCH_NOT_ATTEMPTED = "not_attempted"

DISPATCH_RESULTS = frozenset(
    {
        DISPATCH_GATEWAY_NOTIFIED,
        DISPATCH_WORKER_DISPATCHED,
        DISPATCH_SKIPPED,
        DISPATCH_NOT_ATTEMPTED,
    }
)


# ---------------------------------------------------------------------------
# Value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActuationStep:
    """One ordered actuation step and the status of its (attempted) side effect.

    ``command`` is the concrete, replayable shell command when the step maps to one
    (``git worktree add`` / ``handoff send`` / ``cockpit append``); ``None`` for a read-
    back / confirm step whose exact form is runtime-resolved.
    """

    order: int
    title: str
    status: str
    detail: str
    command: Optional[str] = None

    def as_payload(self) -> dict[str, object]:
        return {
            "order": self.order,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "command": self.command,
        }


@dataclass(frozen=True)
class SublaneActuationOutcome:
    """The machine-readable result of a ``sublane start`` plan / actuation.

    ``status`` is one of :data:`ACTUATE_STATES`. ``execute`` records whether live
    actuation was requested (``False`` = dry-run). The identity / evidence fields
    (``gateway_pane`` / ``worker_pane`` / ``worktree_path`` / ``branch`` /
    ``dispatch_target``) carry the runtime evidence the acceptance wants recorded — the
    same lane a subsequent ``sublane list --json`` shows. ``adopted`` is ``True`` when an
    already-live lane was reused rather than created. ``blocked_reasons`` is the fail-
    closed reason set (empty unless :data:`ACTUATE_BLOCKED`).
    """

    status: str
    execute: bool
    reason: str
    issue: str
    lane_label: str
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    launch_action: Optional[str] = None
    gateway_pane: Optional[str] = None
    worker_pane: Optional[str] = None
    dispatch_target: Optional[str] = None
    dispatch_result: str = DISPATCH_NOT_ATTEMPTED
    durable_anchor: Optional[str] = None
    adopted: bool = False
    steps: Tuple[ActuationStep, ...] = ()
    blocked_reasons: Tuple[str, ...] = ()
    # #13290 dispatch admission gate: the concrete FILL_* token the caller-supplied
    # fill decision resolved to (``None`` when the gate was not armed), and the
    # explicit override reason recorded when a stop was intentionally proceeded past.
    fill_decision: Optional[str] = None
    fill_override_reason: Optional[str] = None
    # #13293 gateway readiness wait: whether the freshly-launched gateway TUI was
    # observed ready (codex foreground process + rendered pane) before the dispatch.
    # ``True`` = confirmed ready and dispatched into a ready composer; ``False`` = the
    # bounded wait elapsed unconfirmed and the dispatch proceeded anyway (the queue-
    # enter rail never hard-blocks — the handoff Enter-only retry is the landing safety
    # net, and the coordinator watches for a no-progress lane); ``None`` = not probed
    # (dry-run, ``--no-dispatch``, wait disabled, or actuation blocked before dispatch).
    gateway_ready: Optional[bool] = None

    @property
    def is_blocked(self) -> bool:
        return self.status == ACTUATE_BLOCKED

    @property
    def executed(self) -> bool:
        return self.status == ACTUATE_EXECUTED

    @property
    def worker_dispatch_confirmed(self) -> bool:
        """True only when the same-lane worker receipt was confirmed.

        A :data:`DISPATCH_GATEWAY_NOTIFIED` dispatch is NOT worker-confirmed: the
        gateway pane received the notification but the same-lane Claude worker
        start is unproven (Redmine #12986). The coordinator reads this to avoid
        treating a gateway-notified lane as worker-started.
        """
        return self.dispatch_result == DISPATCH_WORKER_DISPATCHED

    def as_payload(self) -> dict[str, object]:
        payload = {
            "status": self.status,
            "execute": self.execute,
            "reason": self.reason,
            "issue": self.issue,
            "lane_label": self.lane_label,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "launch_action": self.launch_action,
            "gateway_pane": self.gateway_pane,
            "worker_pane": self.worker_pane,
            "dispatch_target": self.dispatch_target,
            "dispatch_result": self.dispatch_result,
            "worker_dispatch_confirmed": self.worker_dispatch_confirmed,
            "durable_anchor": self.durable_anchor,
            "adopted": self.adopted,
            "steps": [s.as_payload() for s in self.steps],
            "blocked_reasons": list(self.blocked_reasons),
            "fill_decision": self.fill_decision,
            "fill_override_reason": self.fill_override_reason,
            "gateway_ready": self.gateway_ready,
        }
        if self.is_blocked and "sender_attestation" in self.blocked_reasons:
            payload["next_action"] = {
                "action": "restore_attested_coordinator_shell",
                "allowed_methods": [
                    "relaunch_from_fixed_session_start",
                    "verified_high_level_coordinator_proxy",
                ],
                "forbidden_methods": [
                    "manual_mozyo_env_injection",
                    "raw_herdr_send",
                ],
            }
        return payload


def render_actuation_journal(outcome: SublaneActuationOutcome) -> str:
    """Render the actuation outcome as a replayable durable-record snippet (pure).

    This is the "Redmine durable record package" of the issue scope: a machine-readable
    pointer the coordinator posts to the durable anchor. It carries the lane identity and
    the resolved gateway / worker pane evidence (the acceptance wants the created / adopted
    lane confirmable as issue / gateway / worker / branch / state), the launch action, and
    the dispatch outcome. On a fail-closed run it records the blocked reasons and the next
    owner instead of a partial-success claim.
    """
    heading = (
        "## sublane actuation blocked"
        if outcome.is_blocked
        else (
            "## sublane actuated"
            if outcome.execute
            else "## sublane actuation plan (dry-run)"
        )
    )
    lines = [
        heading,
        "",
        f"- issue: #{outcome.issue}",
        f"- lane_label: {outcome.lane_label or '-'}",
        f"- state: {outcome.status}",
        f"- execute: {str(outcome.execute).lower()}",
        f"- adopted: {str(outcome.adopted).lower()}",
        f"- launch_action: {outcome.launch_action or '-'}",
        f"- branch: {outcome.branch or '-'}",
        # #13368: the durable record is pasted into a Redmine journal; render the
        # portable lane worktree sibling basename, never the host-local absolute path
        # (the absolute path stays in `as_payload()["worktree_path"]`, a local surface).
        f"- worktree: {portable_worktree_label(outcome.worktree_path)}",
        f"- gateway_pane: {outcome.gateway_pane or '-'}",
        f"- worker_pane: {outcome.worker_pane or '-'}",
        f"- dispatch_target: {outcome.dispatch_target or '-'}",
        f"- dispatch_result: {outcome.dispatch_result}",
        f"- worker_dispatch_confirmed: {str(outcome.worker_dispatch_confirmed).lower()}",
        f"- durable_anchor: {outcome.durable_anchor or '-'}",
    ]
    # #13290: record the consulted fill decision and any explicit override so the
    # durable record carries the admission decision (reason + anchor) that let a
    # stop-classified dispatch proceed. Emitted only when the gate was armed, so the
    # not-armed / back-compat path stays byte-for-byte unchanged.
    if outcome.fill_decision is not None:
        lines.append(f"- fill_decision: {outcome.fill_decision}")
    if outcome.fill_override_reason is not None:
        lines.append(f"- fill_stop_override: {outcome.fill_override_reason}")
    # #13293: record the pre-dispatch gateway readiness observation only when the wait
    # actually ran (not dry-run / --no-dispatch / disabled), so the back-compat record
    # stays byte-for-byte unchanged.
    if outcome.gateway_ready is not None:
        lines.append(f"- gateway_ready: {str(outcome.gateway_ready).lower()}")
    if outcome.is_blocked:
        lines.append("- blocked_reasons: " + ", ".join(outcome.blocked_reasons))
        if "sender_attestation" in outcome.blocked_reasons:
            lines.append(
                "- next_action: restore an attested coordinator shell by relaunching "
                "from fixed session-start, or use a verified high-level coordinator "
                "proxy; do not inject MOZYO env manually or use raw herdr send"
            )
        else:
            lines.append(
                "- next_action: coordinator callback (fail-closed; lane not fully actuated)"
            )
    else:
        lines.append("- next_action: " + _next_action(outcome))
    return "\n".join(lines)


def _next_action(outcome: SublaneActuationOutcome) -> str:
    """Honest next-action for a non-blocked outcome (pure).

    Redmine #12986: a :data:`DISPATCH_GATEWAY_NOTIFIED` executed run must NOT tell
    the coordinator the gateway already routed to the worker — that overstatement
    is exactly what let #12982 / #12984 read as started while the worker never
    received the request. For a gateway-notified lane the record spells out that
    worker dispatch is unconfirmed and points at the ``callback-recovery``
    classifier so a silent stall is recoverable instead of mistaken for success.
    """
    if not outcome.execute:
        return "re-run with --execute to actuate the resolved plan"
    if outcome.dispatch_result == DISPATCH_GATEWAY_NOTIFIED:
        return (
            "gateway notified only — worker dispatch NOT yet confirmed. The "
            "gateway must forward the implementation_request to the same-lane "
            "worker with the #12988 ack drive (`mozyo-bridge sublane "
            "dispatch-worker --execute`) so a measured worker-dispatch ack "
            "lands; until then treat this as a `no_progress_after_handoff` "
            "candidate (classify with `mozyo-bridge sublane callback-recovery "
            "--dispatch-delivered`). Confirm the lane with `sublane list --json`"
        )
    if outcome.dispatch_result == DISPATCH_WORKER_DISPATCHED:
        return (
            "worker dispatch confirmed; confirm the lane with `sublane list "
            "--json` and await the worker's implementation_done callback"
        )
    if outcome.dispatch_result == DISPATCH_SKIPPED:
        return (
            "lane created/adopted; dispatch skipped (--no-dispatch). Dispatch the "
            "implementation_request to the gateway when ready"
        )
    return "confirm the lane with `sublane list --json`"


__all__ = (
    "STEP_EXECUTED",
    "STEP_READY",
    "STEP_SKIPPED",
    "STEP_BLOCKED",
    "STEP_STATES",
    "ACTUATE_EXECUTED",
    "ACTUATE_READY",
    "ACTUATE_BLOCKED",
    "ACTUATE_STATES",
    "REASON_SENDER_UNATTESTED",
    "REASON_MISSING_IDENTITY",
    "REASON_LAUNCH_BLOCKED",
    "REASON_ANCHOR_REQUIRED",
    "REASON_WORKTREE_CREATE_FAILED",
    "REASON_PANE_CREATE_FAILED",
    "REASON_STAMP_FAILED",
    "REASON_LANE_MISMATCH",
    "REASON_HANDOFF_FAILED",
    "REASON_WORK_UNIT_BLOCKED",
    "REASON_PAIR_SPLIT",
    "REASON_RUNTIME_FINGERPRINT",
    "REASON_FILL_STOP",
    "BLOCKED_REASONS",
    "DISPATCH_GATEWAY_NOTIFIED",
    "DISPATCH_WORKER_DISPATCHED",
    "DISPATCH_SKIPPED",
    "DISPATCH_NOT_ATTEMPTED",
    "DISPATCH_RESULTS",
    "ActuationStep",
    "SublaneActuationOutcome",
    "render_actuation_journal",
)
