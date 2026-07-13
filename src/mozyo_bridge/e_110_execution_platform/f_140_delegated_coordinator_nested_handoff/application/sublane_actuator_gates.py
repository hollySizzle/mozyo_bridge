"""Mutating-actuation admission gates for the sublane use case (Redmine #13705).

Two fail-closed checks the :class:`~.sublane_actuator_use_case.SublaneActuateUseCase`
runs around a live actuation, carved out of the use-case module so it stays under the
module-health ceiling (same pattern as :mod:`.sublane_actuator_heal`). Each drives the
use case as a collaborator (its ``_blocked`` builder) and returns a terminal
:class:`SublaneActuationOutcome` when it fails closed, or ``None`` to proceed:

* :func:`runtime_placement_gate` — the action-time runtime fingerprint front door
  (R1-F1): the official mutating entry goes zero-write when the active runtime is a
  source/installed skew missing the same-tab placement behavior the repo-local source
  ships (the exact class that split the #13441 lane). Optional / herdr-only.
* :func:`pair_split_admission` — the operable-pair check (R1-F2): a resolved lane whose
  gateway/worker pair is ``pair_split`` (both panes live but not co-located) is a
  degraded state, not a healthy pair, so it is never adopted / dispatched.
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    REASON_PAIR_SPLIT,
    REASON_RUNTIME_FINGERPRINT,
    STEP_BLOCKED,
    ActuationStep,
    SublaneActuationOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    SUBLANE_STATE_PAIR_SPLIT,
)


def runtime_placement_gate(
    use_case,
    request,
    *,
    launch_action,
    dispatch: bool,
    fill_decision,
    fill_override_reason,
) -> Optional[SublaneActuationOutcome]:
    """Action-time runtime fingerprint gate — the mutating front door (R1-F1).

    Optional / herdr-only: the port exposes ``preflight_runtime_placement_gate`` only
    when it can compare the active runtime against the repo-local source (the tmux and
    test ports omit it, so this is a no-op there). Returns a blocked outcome when the
    gate refuses — the active runtime is missing the same-tab placement behavior the
    source ships — else ``None``. Runs before EVERY side effect (worktree / append /
    dispatch), so an incompatible runtime never actuates a lane it would misplace.
    """
    gate = getattr(use_case.ops, "preflight_runtime_placement_gate", None)
    if not callable(gate):
        return None
    gate_ok, gate_detail = gate()
    if gate_ok:
        return None
    return use_case._blocked(
        request,
        launch_action=launch_action,
        reason=f"runtime placement fingerprint gate failed before actuation; {gate_detail}",
        reasons=(REASON_RUNTIME_FINGERPRINT,),
        dispatch=dispatch,
        fill_decision=fill_decision,
        fill_override_reason=fill_override_reason,
    )


def pair_split_admission(
    use_case,
    request,
    lane,
    *,
    launch_action,
    dispatch: bool,
    adopted: bool,
    steps: list,
    fill_decision,
    fill_override_reason,
) -> Optional[SublaneActuationOutcome]:
    """Admit only an operable same-tab pair (R1-F2); a ``pair_split`` lane fails closed.

    ``read_lane`` reports a lane whose gateway/worker panes are both live but NOT
    co-located (split across tabs / workspaces) as ``pair_split``. The adopt path (both
    panes present) would otherwise treat it as ``active`` and dispatch to a split lane.
    Returns a blocked outcome (zero stamp / dispatch) for a split lane — recovery is an
    owner-decided retire + recreate, never a normal ``start --execute`` heal-over — else
    ``None``. Covers both read-back sites (adopt + append); the append path never reaches
    here split because the herdr postcondition already raised.
    """
    if lane is None or lane.state != SUBLANE_STATE_PAIR_SPLIT:
        return None
    steps.append(
        ActuationStep(
            order=3,
            title="confirm operable pair",
            status=STEP_BLOCKED,
            detail=f"the resolved lane's gateway {lane.gateway_pane} and worker "
            f"{lane.worker_pane} are split across tabs / workspaces (state=pair_split); "
            "refusing to adopt / dispatch to a lane that is not an operable same-tab "
            "pair (Redmine #13705)",
            command=None,
        )
    )
    return use_case._blocked(
        request,
        launch_action=launch_action,
        reason="resolved lane is a split gateway/worker pair (pair_split), not an "
        "operable same-tab pair; fail-closed before dispatch — retire and recreate the "
        "lane rather than dispatching to a split topology",
        reasons=(REASON_PAIR_SPLIT,),
        dispatch=dispatch,
        steps=tuple(steps),
        gateway_pane=lane.gateway_pane,
        worker_pane=lane.worker_pane,
        adopted=adopted,
        fill_decision=fill_decision,
        fill_override_reason=fill_override_reason,
    )


__all__ = ("pair_split_admission", "runtime_placement_gate")
