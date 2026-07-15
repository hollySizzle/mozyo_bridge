"""CLI execution leg for the bounded herdr worker dispatch (Redmine #13489 increment 2).

When ``workflow step`` resolves a gateway lane to the executable
:data:`...workflow_step.PRIMITIVE_HERDR_DISPATCH_WORKER` leg, this module performs the fenced
one-step dispatch. It **re-resolves the dispatch decision at action time** (a fresh
source-of-truth Redmine authorization read + a fresh exact-target runtime observation) so a
supersede / drift / mid-turn transition between resolution and execution turns into zero send,
then drives the idempotency fence around exactly one real send
(:func:`...herdr_dispatch_execution.execute_dispatch`).

The real send is the existing governed same-lane worker forward
(:class:`HerdrWorkerDispatchOps.dispatch_to_worker`; its exit code is the delivery ACK). The
send factory is injectable so an integration test drives the whole leg hermetically. Product
runtime auto-dispatch stays disabled until a coordinator records a real dispatch authorization
(j#75006 "Important distinction"): absent one, :func:`resolve_dispatch_decision` decides
MONITOR and this leg is never reached.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable, Mapping, Optional

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    DispatchOutboxFenceError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (
    DISPATCH_FENCE_UNAVAILABLE,
    DISPATCH_SKIPPED,
    TURN_START_NOT_STARTED,
    TURN_START_STARTED,
    TURN_START_UNKNOWN,
    DispatchExecutionResult,
    SendOutcome,
    execute_dispatch,
)

# The herdr ops turn-start token -> the fence's SendOutcome turn-start token (mid-review F2).
_OPS_TURN_START_TO_SEND = {
    "started": TURN_START_STARTED,
    "delivered_not_started": TURN_START_NOT_STARTED,
    "not_started": TURN_START_NOT_STARTED,
    "unknown": TURN_START_UNKNOWN,
}
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authority import (
    AUTHORIZE,
    DispatchDecision,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (
    DispatchAuthorization,
)

# Injection seam: builds the single send callable for an authorization (real dispatch by default).
SendFactory = Callable[
    [argparse.Namespace, DispatchAuthorization, str, str, Mapping[str, str]],
    Callable[[], SendOutcome],
]


def _anchor_field(durable_anchor: str, key: str) -> str:
    """Extract ``issue`` / ``journal`` from a ``redmine:issue=<id>:journal=<id>`` pointer."""
    s = (durable_anchor or "").strip()
    if not s or s == "none" or not s.startswith("redmine:"):
        return ""
    for field in s.split(":"):
        field = field.strip()
        if field.startswith(key + "="):
            return field[len(key) + 1:].strip()
    return ""


def _resolve_target_locator(target_assigned_name: str, env: Mapping[str, str]) -> str:
    """The live herdr locator for the exact authorized target, or "" (fail-soft)."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
        list_herdr_agent_rows,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
        HerdrSessionStartError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        AGENT_KEY_NAME,
        _agent_locator,
    )

    want = (target_assigned_name or "").strip()
    try:
        rows = list_herdr_agent_rows(env)
    except HerdrSessionStartError:
        return ""
    for row in rows:
        if isinstance(row, Mapping) and str(row.get(AGENT_KEY_NAME, "")).strip() == want:
            return _agent_locator(row)
    return ""


def _default_send_factory(
    args: argparse.Namespace,
    authorization: DispatchAuthorization,
    journal: str,
    repo_root: str,
    env: Mapping[str, str],
) -> Callable[[], SendOutcome]:
    """Build the single real send: the governed same-lane worker forward (measured ACK)."""

    def _send() -> SendOutcome:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatch_herdr_ops import (
            HerdrWorkerDispatchOps,
        )

        worker_pane = _resolve_target_locator(authorization.target_assigned_name, env)
        if not worker_pane:
            return SendOutcome(
                turn_start=TURN_START_NOT_STARTED,
                detail="the authorized target locator vanished between decision and send",
            )
        ops = HerdrWorkerDispatchOps(
            repo_root=Path(repo_root),
            lane_label=authorization.lane_id,
            issue=authorization.issue,
            env=dict(env),
        )
        # Surface the structured herdr turn-start FROM the dispatch ops (mid-review j#75047 F2):
        # the delivery ACK alone (submit-completion) is not a turn-start, so `delivered` is
        # confirmed only when the ops observes the exact worker's turn actually started
        # (`started`); ACK-without-turn / unobservable -> uncertain. No raw wait loop.
        rc, ops_turn_start = ops.dispatch_to_worker_turn_start(
            issue=authorization.issue,
            journal=journal,
            worker_pane=worker_pane,
            lane_label=authorization.lane_id,
            gateway_callback_target=None,
            target_repo="auto",
            worker_assigned_name=authorization.target_assigned_name,
            allow_direct_worker=True,
        )
        turn_start = _OPS_TURN_START_TO_SEND.get(ops_turn_start, TURN_START_UNKNOWN)
        return SendOutcome(turn_start=turn_start, detail=f"worker dispatch rc={rc} turn_start={ops_turn_start}")

    return _send


def execute_herdr_dispatch(
    args: argparse.Namespace,
    durable_anchor: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    send_factory: SendFactory = None,  # type: ignore[assignment]
    fence: Optional[DispatchOutboxFence] = None,
) -> DispatchExecutionResult:
    """Perform the fenced one-step dispatch for a resolved gateway dispatch leg (action-time).

    Re-resolves the dispatch decision from source-of-truth Redmine + the live target runtime; a
    non-AUTHORIZE decision (superseded / drifted / mid-turn since resolution) is zero send
    (:data:`DISPATCH_SKIPPED`). On AUTHORIZE, reserves the idempotency fence and performs at most
    one real send, writing the outcome. ``send_factory`` / ``fence`` are injectable for hermetic
    tests; by default the send is the live worker dispatch and the fence is the home store.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_authority import (
        resolve_dispatch_decision,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_workflow_step import (
        _anchor_workspace_id,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
        resolve_sender_identity,
    )

    environ = env if env is not None else os.environ
    repo_root = repo_root_from_args(args)
    issue = _anchor_field(durable_anchor, "issue")
    journal = _anchor_field(durable_anchor, "journal")

    sender_res = resolve_sender_identity(environ, anchor_workspace_id=_anchor_workspace_id(repo_root))
    if not sender_res.ok or sender_res.identity is None or not issue:
        return DispatchExecutionResult(
            result=DISPATCH_SKIPPED,
            fence_state="absent",
            detail="lane identity / anchor unresolved at action time; no send",
            sent=False,
        )
    sender = sender_res.identity

    decision: DispatchDecision = resolve_dispatch_decision(
        args,
        workspace_id=sender.workspace_id,
        lane_id=sender.lane_id,
        issue=issue,
        env=environ,
    )
    if decision.decision != AUTHORIZE or decision.authorization is None:
        return DispatchExecutionResult(
            result=DISPATCH_SKIPPED,
            fence_state="absent",
            detail=(
                "the dispatch decision is no longer AUTHORIZE at action time "
                f"({decision.decision}: {decision.reason}); no send"
            ),
            sent=False,
        )

    authorization = decision.authorization
    outbox = fence if fence is not None else DispatchOutboxFence()
    # Auto-bootstrap the identity: this is deletion-safe (mid-review j#75047 F1). A genuine first
    # bootstrap creates the DB + sidecar; a store loss (sidecar remains, DB gone) makes bootstrap
    # refuse -> fail closed here (zero send). It never silently re-creates a fresh empty store.
    try:
        outbox.bootstrap()
    except DispatchOutboxFenceError as exc:
        return DispatchExecutionResult(
            result=DISPATCH_FENCE_UNAVAILABLE,
            fence_state="absent",
            detail=f"dispatch fence unavailable ({exc}); no send — operator recover() required",
            sent=False,
        )
    factory = send_factory if send_factory is not None else _default_send_factory
    send = factory(args, authorization, journal, str(repo_root), environ)
    return execute_dispatch(authorization=authorization, fence=outbox, send=send)


__all__ = ("execute_herdr_dispatch",)
