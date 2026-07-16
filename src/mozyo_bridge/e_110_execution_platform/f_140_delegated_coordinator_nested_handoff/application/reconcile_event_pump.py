"""Supervisor-owned event pump for the event-driven reconciler (Redmine #13758 Q1, j#79507).

The event-driven PRIMARY activation the reconciler needs: instead of only the bounded
StartInterval sweep (which cannot observe the ``busy -> turn_ended`` transient), the
``WorkspaceCallbackSupervisor`` — the SOLE reconcile owner — is driven by Herdr turn events.
Each bounded iteration runs one supervisor pass (which observes the live runtime + reconciles
+ re-reads Redmine), enumerates the active-lane expected-owner targets, and arms a bounded
MULTIPLEX Herdr ``wait agent-status --status done`` (the raw status mozyo maps to
``turn_ended``; NOT the ``working`` default used for turn-START). On any event / timeout /
error it proceeds — a single target never blocks the others and loses their edges (Design
Answer j#79507 Q1).

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
    """Arm a bounded wait per target and return the FIRST that wakes (or a timeout). (multiplex)

    Round-robins one bounded :mod:`...callback_wake` wait per target and returns the first target
    that OBSERVES a ``busy -> turn_ended`` change (``WAKE_WOKE``), so a single target's bounded
    wait never blocks the others (Design Answer j#79507 Q1 point 2). If none woke, returns the
    LAST non-woke signal (timeout / error) and no target — the pump then still runs the bounded
    whole-roster reconciliation. Empty targets -> a benign timeout signal.
    """
    last = WakeSignal(kind=WAKE_TIMED_OUT, detail="no_targets")
    for t in targets or ():
        signal = resolve_wake_fn(wait_builder(t), detail=f"{t.workspace_id}:{t.issue}")
        if signal.kind == WAKE_WOKE:
            return signal, t  # first observed turn-end edge wins
        last = signal
    return last, None


def run_event_pump(
    *,
    supervisor_pass: Callable[[str, Sequence[tuple]], object],
    targets_fn: Callable[[], Sequence[EventPumpTarget]],
    wait_multiplex_fn: Callable[[Sequence[EventPumpTarget]], "tuple[WakeSignal, Optional[EventPumpTarget]]"],
    max_iterations: int,
) -> list:
    """Run the bounded supervisor event pump; return one record per iteration.

    Each iteration: (1) run one supervisor pass (bounded reconciliation — observes the live
    runtime, reconciles, re-reads Redmine); (2) enumerate the active-lane targets; (3) arm the
    bounded multiplex wait. A woken target threads its ``(workspace_id, issue)`` as a local-wake
    hint into the NEXT pass so the just-ended turn is reconciled promptly (``local_wake`` mode);
    a timeout / error runs the next bounded whole-roster reconciliation. Bounded by
    ``max_iterations`` so the pump is never an unbounded poll. A pass that raises is recorded and
    the loop survives to its next bounded wait (the outbox / store fences make every pass
    idempotent).
    """
    results: list = []
    hints: Sequence[tuple] = ()
    for _ in range(max(0, int(max_iterations))):
        mode = SUPERVISION_LOCAL_WAKE if hints else SUPERVISION_BOUNDED_RECONCILIATION
        try:
            outcome = supervisor_pass(mode, hints)
            pass_ok = True
        except Exception as exc:  # noqa: BLE001 - a failed pass must not kill the pump
            outcome, pass_ok = {"error": type(exc).__name__}, False
        try:
            targets = list(targets_fn())
        except Exception:  # noqa: BLE001 - an unreadable target set is a benign empty wait
            targets = []
        signal, woken = wait_multiplex_fn(targets)
        # A woken target's (workspace, issue) becomes the next pass's local-wake hint.
        hints = ((woken.workspace_id, woken.issue),) if woken is not None else ()
        results.append(
            {
                "mode": mode,
                "pass_ok": pass_ok,
                "wake": signal.kind,
                "woke_target": woken.target if woken is not None else "",
            }
        )
    return results


def build_event_pump_seams(
    *,
    supervisor,
    targets_fn: Callable[[], Sequence[EventPumpTarget]],
    wait_binary: str = "mozyo-bridge",
    timeout_ms: int,
    wait_runner=None,
) -> "tuple[Callable, Callable, Callable]":
    """Build the production ``(supervisor_pass, targets_fn, wait_multiplex_fn)`` for :func:`run_event_pump`.

    - ``supervisor_pass(mode, hints)`` drives the shared :class:`WorkspaceCallbackSupervisor`
      (the sole reconcile owner) — no second supervisor;
    - ``targets_fn`` enumerates the active-lane expected-owner Herdr targets (injected);
    - the multiplex wait arms a bounded :mod:`...callback_wake` ``wait agent-status --status done``
      per target (the turn_ended raw status), reusing the stable Herdr wait primitive.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_wake import (
        build_herdr_event_wait,
    )

    def _pass(mode, hints):
        return supervisor.run_once(mode=mode, wake_hints=hints)

    def _wait_builder(t: EventPumpTarget):
        return build_herdr_event_wait(
            wait_binary, t.target,
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
