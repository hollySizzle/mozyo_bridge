"""Supervisor-owned event pump for the event-driven reconciler (Redmine #13758 Q1, j#79507).

The event-driven PRIMARY activation the reconciler needs: instead of only the bounded
StartInterval sweep (which cannot observe the ``busy -> turn_ended`` transient), the
``WorkspaceCallbackSupervisor`` — the SOLE reconcile owner — is driven by Herdr turn events.
After a startup bootstrap reconcile, each bounded iteration is **wait -> pass**: it enumerates
the active-lane expected-owner targets, arms a bounded CONCURRENT/MULTIPLEX Herdr ``wait
agent-status --status done`` (the raw status mozyo maps to ``turn_ended``; NOT the ``working``
default used for turn-START), then runs one supervisor pass that CONSUMES that outcome (observes
the live runtime + reconciles + re-reads Redmine). So an observed edge is reconciled within the
same bounded invocation — even the CLI default ``--max-iterations 1`` (review R6-F3). On any
event / timeout / error it proceeds — every target's wait is armed together so a single target
never blocks the others and loses their edges (Design Answer j#79507 Q1; review R6-F2). The wait
spawns the sanctioned trusted-environment ``herdr`` binary, never ``mozyo-bridge`` (review R6-F1).

Design invariants (j#79507 Q1):

- reuse the :mod:`...callback_wake` stable Herdr wait primitive; the wake is a HINT, never
  workflow authority — every pass re-reads the exact Redmine gate / generation / route / outbox;
- the supervisor is the single reconcile owner (no second supervisor / outbox / workflow truth);
  the pass shares the workspace lease + callback outbox + reconcile store;
- bounded by ``max_iterations`` — never an unbounded LLM-turn poll; a timeout / error still runs
  the bounded whole-roster reconciliation (the existing loss-recovery fallback);
- all I/O is injected (the supervisor pass, the target enumeration, the multiplex wait), so the
  pump is deterministically test-pinned without a live Herdr / registry. The production wiring is
  built by :func:`build_event_pump_seams`.
"""

from __future__ import annotations

import concurrent.futures as _futures
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_wake import (
    WAKE_ERROR,
    WAKE_TIMED_OUT,
    WAKE_WOKE,
    WakeSignal,
    resolve_wake,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    SUPERVISION_BOUNDED_RECONCILIATION,
    SUPERVISION_LOCAL_WAKE,
)

#: The raw Herdr status that maps to the mozyo ``turn_ended`` runtime (agent_state
#: ``HERDR_STATUS_DONE``). The reconcile wait waits for a *change into* this status — the
#: busy -> turn_ended edge — NOT the ``working`` turn-START default.
HERDR_STATUS_TURN_ENDED = "done"


@dataclass(frozen=True)
class EventPumpTarget:
    """One Herdr wait target: the active lane's expected-owner assigned Herdr agent name."""

    workspace_id: str
    issue: str
    lane_id: str
    target: str  # the stable assigned Herdr agent name/id for ``wait agent-status``


def multiplex_wait(
    targets: Sequence[EventPumpTarget],
    *,
    wait_builder: Callable[[EventPumpTarget], Callable[[], object]],
    resolve_wake_fn: Callable[..., WakeSignal] = resolve_wake,
) -> "tuple[WakeSignal, Optional[EventPumpTarget]]":
    """Arm every target's bounded wait CONCURRENTLY and return the FIRST that wakes. (multiplex)

    Review R6-F2: a serial loop would block every other target's ``busy -> turn_ended`` edge
    behind the first target's whole bounded wait (N idle targets => N x timeout, and a real edge
    on target 2 is lost while target 1 blocks). So all target waits are armed together on a bounded
    thread pool and the FIRST target that observes the change (``WAKE_WOKE``) wins — a single
    target never blocks the others (Design Answer j#79507 Q1 point 2). The remaining in-flight
    waits are cancelled / detached (each is already bounded by its own ``wait agent-status
    --timeout`` + the outer subprocess timeout, so a detached thread cannot pin the runtime). If
    none woke, the LAST observed non-woke signal (timeout / error) and no target are returned — the
    pump then still runs the bounded whole-roster reconciliation. Empty targets -> a benign timeout.
    """
    tlist = [t for t in (targets or ())]
    if not tlist:
        return WakeSignal(kind=WAKE_TIMED_OUT, detail="no_targets"), None

    def _resolve(t: EventPumpTarget) -> WakeSignal:
        return resolve_wake_fn(wait_builder(t), detail=f"{t.workspace_id}:{t.issue}")

    executor = _futures.ThreadPoolExecutor(max_workers=len(tlist))
    future_target = {executor.submit(_resolve, t): t for t in tlist}
    last = WakeSignal(kind=WAKE_TIMED_OUT, detail="all_pending")
    won = None  # (WakeSignal, EventPumpTarget) of the first observed turn-end edge, if any
    try:
        for fut in _futures.as_completed(future_target):
            t = future_target[fut]
            try:
                signal = fut.result()
            except Exception as exc:  # noqa: BLE001 - a resolver crash is a fail-safe wake error
                signal = WakeSignal(kind=WAKE_ERROR, detail=f"{type(exc).__name__}")
            if signal.kind == WAKE_WOKE:
                won = (signal, t)  # first observed turn-end edge wins
                break
            last = signal
    finally:
        for fut in future_target:
            fut.cancel()
        # Do NOT block on the still-running bounded waits (that would re-serialize the multiplex);
        # each is self-bounded, so detach the pool and let them reap in the background.
        executor.shutdown(wait=False)
    if won is not None:
        return won
    return last, None


def _run_supervisor_pass(
    supervisor_pass: Callable[[str, Sequence[tuple]], object],
    mode: str,
    hints: Sequence[tuple],
) -> dict:
    """Run one supervisor pass fail-safe; a raised pass is recorded, never kills the pump."""
    try:
        supervisor_pass(mode, hints)
        return {"mode": mode, "pass_ok": True}
    except Exception as exc:  # noqa: BLE001 - a failed pass must not kill the pump
        return {"mode": mode, "pass_ok": False, "error": type(exc).__name__}


def run_event_pump(
    *,
    supervisor_pass: Callable[[str, Sequence[tuple]], object],
    targets_fn: Callable[[], Sequence[EventPumpTarget]],
    wait_multiplex_fn: Callable[[Sequence[EventPumpTarget]], "tuple[WakeSignal, Optional[EventPumpTarget]]"],
    max_iterations: int,
) -> list:
    """Run the bounded supervisor event pump; return one record per supervisor pass.

    Review R6-F3: an observed wake MUST be consumed by a supervisor reconcile within the SAME
    bounded invocation — not deferred to a "next" pass that the CLI default (``--max-iterations
    1``) never runs. So each iteration is **wait -> pass**: (1) enumerate the active-lane targets;
    (2) arm the bounded multiplex wait; (3) run exactly one supervisor pass that CONSUMES that
    outcome — a woken target's ``(workspace_id, issue)`` drives a ``local_wake`` pass, a timeout /
    error drives the bounded whole-roster reconciliation. A single ``--max-iterations 1`` run
    therefore observes an edge and reconciles it before returning.

    A startup **bootstrap** pass (bounded whole-roster reconciliation) runs once before the first
    wait so already-outstanding work is reconciled promptly instead of waiting a whole timeout
    window for the first edge. It is not counted against ``max_iterations``. Every pass re-reads
    the exact Redmine gate / generation / route / outbox (the wake is only a hint), and the
    outbox / store fences make every pass idempotent, so a duplicate / persistent-done event folds
    into the same state. Bounded by ``max_iterations`` so the pump is never an unbounded poll.
    """
    results: list = []
    n = max(0, int(max_iterations))
    if n <= 0:
        return results
    # Startup bootstrap: reconcile already-outstanding work before waiting for the first edge.
    boot = _run_supervisor_pass(supervisor_pass, SUPERVISION_BOUNDED_RECONCILIATION, ())
    boot.update({"wake": "bootstrap", "woke_target": ""})
    results.append(boot)
    for _ in range(n):
        try:
            targets = list(targets_fn())
        except Exception:  # noqa: BLE001 - an unreadable target set is a benign empty wait
            targets = []
        signal, woken = wait_multiplex_fn(targets)
        # The wake (or timeout) is consumed by THIS iteration's pass, not a deferred next one.
        hints: Sequence[tuple] = ((woken.workspace_id, woken.issue),) if woken is not None else ()
        mode = SUPERVISION_LOCAL_WAKE if hints else SUPERVISION_BOUNDED_RECONCILIATION
        rec = _run_supervisor_pass(supervisor_pass, mode, hints)
        rec.update({"wake": signal.kind, "woke_target": woken.target if woken is not None else ""})
        results.append(rec)
    return results


def build_event_pump_seams(
    *,
    supervisor,
    targets_fn: Callable[[], Sequence[EventPumpTarget]],
    wait_binary: str,
    timeout_ms: int,
    wait_runner=None,
) -> "tuple[Callable, Callable, Callable]":
    """Build the production ``(supervisor_pass, targets_fn, wait_multiplex_fn)`` for :func:`run_event_pump`.

    - ``supervisor_pass(mode, hints)`` drives the shared :class:`WorkspaceCallbackSupervisor`
      (the sole reconcile owner) — no second supervisor;
    - ``targets_fn`` enumerates the active-lane expected-owner Herdr targets (injected);
    - the multiplex wait arms a bounded :mod:`...callback_wake` ``wait agent-status --status done``
      per target (the turn_ended raw status), reusing the stable Herdr wait primitive.

    ``wait_binary`` MUST be the sanctioned trusted-environment herdr executable — resolved by the
    composition root via :func:`resolve_herdr_binary` (review R6-F1). It is ``herdr`` (whose
    ``wait agent-status`` surface this uses), never ``mozyo-bridge`` (which has no ``wait``
    subcommand). When the herdr binary is not configured in the trusted environment the composition
    root passes an empty ``wait_binary``: the pump then degrades to a TIMEOUT-ONLY wait (no live
    event source) so it still runs the bounded whole-roster reconciliation each iteration rather
    than spawning a bogus executable.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_wake import (
        build_herdr_event_wait,
    )

    binary = str(wait_binary or "").strip()

    def _pass(mode, hints):
        return supervisor.run_once(mode=mode, wake_hints=hints)

    def _wait_builder(t: EventPumpTarget):
        if not binary:
            # No trusted herdr binary -> no event source: a benign bounded timeout (still re-reads).
            return lambda: False
        return build_herdr_event_wait(
            binary, t.target,
            status=HERDR_STATUS_TURN_ENDED, timeout_ms=int(timeout_ms), runner=wait_runner,
        )

    def _wait_multiplex(targets):
        return multiplex_wait(targets, wait_builder=_wait_builder)

    return _pass, targets_fn, _wait_multiplex


def pump_targets_from(agents: Iterable[object], lane_issue_fn: Callable[[str, str], str]) -> list:
    """Build ``EventPumpTarget``s from observed agents + a ``(ws, lane) -> issue`` resolver. (pure)

    One target per MANAGED observed agent in an ACTIVE lane (the resolver returns a non-empty
    issue only for an active lane it owns): the wait target is the agent's assigned Herdr name.
    Split out so the enumeration is test-pinned against production-shape observed-agent records.
    Unmanaged / unresolved agents are skipped (fail-open — the pump waits on fewer targets).
    """
    targets: list = []
    for a in agents or ():
        if not getattr(a, "managed", False):
            continue
        ws = str(getattr(a, "workspace_id", "") or "").strip()
        lane = str(getattr(a, "lane_id", "") or "").strip()
        name = str(getattr(a, "name", "") or "").strip()
        if not (ws and lane and name):
            continue
        try:
            issue = str(lane_issue_fn(ws, lane) or "").strip()
        except Exception:  # noqa: BLE001 - an unresolved lane is a fail-open skip
            issue = ""
        if not issue:
            continue
        targets.append(
            EventPumpTarget(workspace_id=ws, issue=issue, lane_id=lane, target=name)
        )
    return targets


def default_pump_targets(*, home=None) -> list:
    """The production active-lane target enumeration for the event pump (best-effort, fail-open)."""
    try:
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_live_source import (
            _live_observed_agents,
        )

        store = LaneLifecycleStore(home=home)

        def _lane_issue(ws: str, lane: str) -> str:
            try:
                rec = store.get(LaneLifecycleKey(ws, lane))
            except Exception:  # noqa: BLE001 - unreadable lifecycle -> no target
                return ""
            if rec is None:
                return ""
            if str(getattr(rec, "lane_disposition", "") or "").strip() != "active":
                return ""
            return str(getattr(rec, "issue_id", "") or "").strip()

        return pump_targets_from(_live_observed_agents(), _lane_issue)
    except Exception:  # noqa: BLE001 - an unavailable inventory / store -> no targets (fail-open)
        return []


__all__ = (
    "HERDR_STATUS_TURN_ENDED",
    "EventPumpTarget",
    "multiplex_wait",
    "run_event_pump",
    "build_event_pump_seams",
    "pump_targets_from",
    "default_pump_targets",
    "WAKE_WOKE",
    "WAKE_TIMED_OUT",
    "WAKE_ERROR",
)
