"""Pure dispatch-authority decision (Redmine #13489 increment 2).

Combines the three action-time preconditions the design contract requires into a single
fail-closed decision (design ``### Increment 2 dispatch 再有効化 contract``; j#74922 / j#74996 /
j#75001):

1. a valid, non-superseded coordinator dispatch authorization
   (:mod:`...domain.dispatch_authorization`);
2. the action-time runtime state of the **exact** authorized target (the application adapter
   resolves the authorization's ``target_assigned_name`` against the live herdr control-socket
   inventory and folds *cardinality + runtime state* into one :data:`TargetRuntime` token — a
   drifted / renamed target resolves to :data:`TARGET_ABSENT`, a duplicate to
   :data:`TARGET_AMBIGUOUS`);
3. (at execution time, not here) an empty atomic idempotency fence.

This module is **pure**: total functions over the already-resolved authorization + target
token. Only :data:`AUTHORIZE` proceeds to a reserve + send; every other decision is **zero
send** — either :data:`MONITOR` (a no-op: no authorization yet, superseded, or the worker is
mid-turn) or :data:`BLOCKED` (fail-closed: the target could not be trusted / observed). The
runtime token vocabulary is mirrored as literals (like the provider tokens in
:mod:`...domain.workflow_step_herdr`) so this execution-platform domain does not import the
e_140 terminal-runtime adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (
    DispatchAuthorization,
)

# ---------------------------------------------------------------------------
# Resolved-target runtime tokens (cardinality folded with runtime state). Mirrors the e_140
# ``agent_state`` runtime receiver-states plus the cardinality outcomes; kept literal so this
# domain stays inside its bounded context.
# ---------------------------------------------------------------------------
TARGET_AWAITING_INPUT = "awaiting_input"  # exactly one authorized target, idle/awaiting input
TARGET_BUSY = "busy"  # exactly one target, working (implementation in flight)
TARGET_BLOCKED = "blocked"  # exactly one target, blocked
TARGET_TURN_ENDED = "turn_ended"  # exactly one target, turn ended (done)
TARGET_UNKNOWN = "unknown"  # exactly one target, unobservable runtime state
TARGET_ABSENT = "absent"  # the authorized target name is not live (0 rows / drift)
TARGET_AMBIGUOUS = "ambiguous"  # 2+ rows for the authorized target name (duplicate identity)
TARGET_UNAVAILABLE = "unavailable"  # the live inventory could not be read

# ---------------------------------------------------------------------------
# Decision tokens.
# ---------------------------------------------------------------------------
AUTHORIZE = "authorize"  # all preconditions hold -> reserve + one send
MONITOR = "monitor"  # zero send, not an error (no authorization / superseded / mid-turn)
BLOCKED = "blocked"  # zero send, fail-closed (untrusted / unobservable target)

# ---------------------------------------------------------------------------
# Decision reason vocabulary (machine-readable; literal regardless of UI language).
# ---------------------------------------------------------------------------
REASON_NO_AUTHORIZATION = "dispatch_no_authorization"
REASON_AUTHORIZATION_INVALID = "dispatch_authorization_invalid"
REASON_AUTHORIZATION_SUPERSEDED = "dispatch_authorization_superseded"
REASON_TARGET_DRIFT = "dispatch_target_identity_drift"
REASON_RUNTIME_NOT_READY = "dispatch_runtime_not_ready"
REASON_RUNTIME_UNKNOWN = "dispatch_runtime_unknown"
REASON_TARGET_ABSENT = "dispatch_target_absent"
REASON_TARGET_AMBIGUOUS = "dispatch_target_ambiguous"
REASON_RUNTIME_UNAVAILABLE = "dispatch_runtime_unavailable"
REASON_REDMINE_UNAVAILABLE = "dispatch_redmine_unavailable"
REASON_AUTHORIZED = "dispatch_authorized"

# Non-ready single-target runtime states that mean the worker is mid-turn -> monitor, not error.
_MONITOR_RUNTIMES = frozenset({TARGET_BUSY, TARGET_BLOCKED, TARGET_TURN_ENDED})


@dataclass(frozen=True)
class DispatchDecision:
    """The pure decision: one of :data:`AUTHORIZE` / :data:`MONITOR` / :data:`BLOCKED`.

    ``authorization`` is echoed only on :data:`AUTHORIZE` (the executor reads ``action_id`` /
    ``target_assigned_name`` from it); it is ``None`` on every zero-send decision. ``reason`` /
    ``detail`` explain the decision.
    """

    decision: str
    reason: str
    detail: str = ""
    authorization: Optional[DispatchAuthorization] = None

    @property
    def authorized(self) -> bool:
        return self.decision == AUTHORIZE


def decide_dispatch_authority(
    *,
    authorization: Optional[DispatchAuthorization],
    superseded: bool,
    target_runtime: str,
) -> DispatchDecision:
    """Decide whether the gateway may auto-dispatch its worker (pure, fail-closed).

    ``authorization`` is the latest dispatch authorization the adapter selected for this exact
    lane + issue (already lane/issue-correlated), or ``None`` when the source-of-truth Redmine
    read found none. ``superseded`` is whether a later durable gate on the issue overrides it.
    ``target_runtime`` is the folded cardinality+state token for the authorization's **exact**
    ``target_assigned_name`` (:data:`TARGET_AWAITING_INPUT` … :data:`TARGET_UNAVAILABLE`).

    Only an authorization that is present, valid, not superseded, and whose exact target is a
    single ``awaiting_input`` slot yields :data:`AUTHORIZE`. Everything else is zero send.
    """
    if authorization is None:
        return DispatchDecision(
            MONITOR,
            REASON_NO_AUTHORIZATION,
            "no coordinator dispatch authorization on the source-of-truth Redmine issue; "
            "auto-dispatch stays disabled until one is recorded",
        )
    if not authorization.valid:
        return DispatchDecision(
            BLOCKED,
            REASON_AUTHORIZATION_INVALID,
            "a dispatch-authorization marker is present but malformed / missing a required "
            "field or an exact authority value (action=dispatch_worker, conclusion=authorized, "
            "target_role=implementation_worker, authorized_by_role=coordinator); fail closed",
        )
    if superseded:
        return DispatchDecision(
            MONITOR,
            REASON_AUTHORIZATION_SUPERSEDED,
            "the dispatch authorization is superseded by a later durable gate "
            "(implementation_done / review / close / blocked); monitor rather than re-dispatch",
        )
    if target_runtime == TARGET_AWAITING_INPUT:
        return DispatchDecision(
            AUTHORIZE,
            REASON_AUTHORIZED,
            "valid non-superseded authorization and the exact target is a single "
            "awaiting_input worker slot",
            authorization=authorization,
        )
    if target_runtime in _MONITOR_RUNTIMES:
        return DispatchDecision(
            MONITOR,
            REASON_RUNTIME_NOT_READY,
            f"the authorized target is live but {target_runtime!r} (mid-turn), not "
            "awaiting_input; monitor rather than dispatch",
        )
    if target_runtime == TARGET_ABSENT:
        return DispatchDecision(
            BLOCKED,
            REASON_TARGET_ABSENT,
            "the authorization's exact target_assigned_name is not a live worker slot "
            "(absent / identity drift); fail closed rather than dispatch to a guessed target",
        )
    if target_runtime == TARGET_AMBIGUOUS:
        return DispatchDecision(
            BLOCKED,
            REASON_TARGET_AMBIGUOUS,
            "2+ live slots decode to the authorization's target_assigned_name (duplicate "
            "identity); fail closed rather than pick one",
        )
    if target_runtime == TARGET_UNAVAILABLE:
        return DispatchDecision(
            BLOCKED,
            REASON_RUNTIME_UNAVAILABLE,
            "the live herdr inventory could not be read to observe the target runtime state; "
            "fail closed",
        )
    # TARGET_UNKNOWN or any unrecognized token: unobservable -> fail closed.
    return DispatchDecision(
        BLOCKED,
        REASON_RUNTIME_UNKNOWN,
        "the authorized target's runtime state is unknown / unobservable; runtime readiness is "
        "a necessary condition and cannot be assumed",
    )


__all__ = (
    "TARGET_AWAITING_INPUT",
    "TARGET_BUSY",
    "TARGET_BLOCKED",
    "TARGET_TURN_ENDED",
    "TARGET_UNKNOWN",
    "TARGET_ABSENT",
    "TARGET_AMBIGUOUS",
    "TARGET_UNAVAILABLE",
    "AUTHORIZE",
    "MONITOR",
    "BLOCKED",
    "REASON_NO_AUTHORIZATION",
    "REASON_AUTHORIZATION_INVALID",
    "REASON_AUTHORIZATION_SUPERSEDED",
    "REASON_TARGET_DRIFT",
    "REASON_RUNTIME_NOT_READY",
    "REASON_RUNTIME_UNKNOWN",
    "REASON_TARGET_ABSENT",
    "REASON_TARGET_AMBIGUOUS",
    "REASON_RUNTIME_UNAVAILABLE",
    "REASON_REDMINE_UNAVAILABLE",
    "REASON_AUTHORIZED",
    "DispatchDecision",
    "decide_dispatch_authority",
)
