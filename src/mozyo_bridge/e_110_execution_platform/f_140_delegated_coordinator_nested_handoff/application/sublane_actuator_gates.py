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
    REASON_PARTIAL_PAIR_RECOVERY,
    REASON_RUNTIME_FINGERPRINT,
    STEP_BLOCKED,
    STEP_EXECUTED,
    ActuationStep,
    SublaneActuationOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.pair_launch_attestation import (  # noqa: E501
    decide_pair_launch_attestation,
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


def pair_attestation_admission(
    use_case,
    request,
    *,
    launch_action,
    dispatch: bool,
    adopted: bool,
    gateway_pane,
    worker_pane,
    lane_runtime_root: str,
    steps: list,
    fill_decision,
    fill_override_reason,
) -> Optional[SublaneActuationOutcome]:
    """Confirm BOTH freshly-launched slots self-attested, else fail closed (Redmine #13847).

    A launch that returns a live locator is not proof the pair attested: an incompatible
    launcher (schema skew — closed pre-launch by the capability preflight) or a failed env
    injection leaves a slot **live but unattested / stale**, and promoting that to
    ``executed`` is the false success #13847 closes (live evidence: gateway ``unattested``,
    worker ``stale_named_slot``). This is the action-time confirmation *after* launch.

    Scope: FRESH launches only (``adopted`` is False). An adopt of an already-live pair is
    validated by the owner-declaration gate, and its slots attested at their own earlier
    launch, so re-requiring a post-this-action attestation would wrongly block a healthy
    adopt. Optional / herdr-only: the port exposes ``observe_pair_attestation`` only where
    it can read the attestation store (the tmux + test ports omit it → no-op), so this is
    back-compatible.

    The self-attestation is written by the wrapper BEFORE the provider exec, so it lands
    early; a bounded poll (reusing the gateway-readiness cadence) tolerates the herdr
    registration lag. Both slots must reach the positive ``ATTEST_OK`` join (freshness for a
    fresh launch is proven by the live-locator match); any non-ok slot after the window
    yields a ``partial_pair_recovery_required`` block whose step names the bad roles and the
    durable recovery pointer (the public ``sublane recover-pair`` surface), never
    ``executed``.
    """
    if adopted:
        return None
    observe = getattr(use_case.ops, "observe_pair_attestation", None)
    if not callable(observe):
        return None
    probes = max(1, use_case.gateway_ready_probes)
    verdict = None
    for attempt in range(probes):
        observation = observe(lane_runtime_root)
        if observation is None:
            # Wrapping inactive (unwrapped byte-invariant launch, #13637): no attestation
            # is expected, so there is nothing to confirm — proceed (the read side is the
            # fail-closed net). Never treated as a partial failure.
            return None
        gateway_slot, worker_slot = observation
        verdict = decide_pair_launch_attestation(gateway_slot, worker_slot)
        if verdict.ok:
            steps.append(
                ActuationStep(
                    order=3,
                    title="confirm pair self-attestation",
                    status=STEP_EXECUTED,
                    detail="both slots produced a fresh, locator-matched startup "
                    f"self-attestation after {attempt + 1} probe(s) "
                    f"(gateway={gateway_slot.state} worker={worker_slot.state})",
                    command=None,
                )
            )
            return None
        if attempt + 1 < probes:
            use_case.sleep(use_case.gateway_ready_interval_seconds)
    # Window elapsed with at least one slot unattested — never a success.
    recover_cmd = (
        f"mozyo-bridge sublane recover-pair --issue {request.issue} "
        f"--lane {request.lane_label}"
    )
    steps.append(
        ActuationStep(
            order=3,
            title="confirm pair self-attestation",
            status=STEP_BLOCKED,
            detail=(
                "the launched pair did not confirm both slots' startup self-attestation "
                f"({verdict.blocked_summary()}); the pair booted partially (live but "
                "unattested/stale). Refusing to report a started lane — recover the exact "
                "pair before resume/dispatch."
            ),
            command=recover_cmd,
        )
    )
    return use_case._blocked(
        request,
        launch_action=launch_action,
        reason="freshly-launched pair did not confirm both slots' post-launch "
        f"self-attestation ({verdict.blocked_summary()}); fail-closed instead of a false "
        "started lane — use the public exact-pair recovery",
        reasons=(REASON_PARTIAL_PAIR_RECOVERY,)
        + tuple(f"unattested:{role}" for role in verdict.blocked_roles),
        dispatch=dispatch,
        steps=tuple(steps),
        gateway_pane=gateway_pane,
        worker_pane=worker_pane,
        adopted=adopted,
        fill_decision=fill_decision,
        fill_override_reason=fill_override_reason,
    )


__all__ = (
    "pair_attestation_admission",
    "pair_split_admission",
    "runtime_placement_gate",
)
