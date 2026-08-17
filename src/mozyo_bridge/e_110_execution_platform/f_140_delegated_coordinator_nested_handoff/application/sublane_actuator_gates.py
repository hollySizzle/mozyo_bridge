"""Mutating-actuation admission gates for the sublane use case (Redmine #13705).

Fail-closed checks the :class:`~.sublane_actuator_use_case.SublaneActuateUseCase` runs
around a live actuation, carved out of the use-case module so it stays under the
module-health ceiling (same pattern as :mod:`.sublane_actuator_heal`). Each drives the
use case as a collaborator (its ``_blocked`` builder) and returns a terminal
:class:`SublaneActuationOutcome` when it fails closed, or ``None`` to proceed:

* :func:`pre_mutation_admission` — everything that must hold before the FIRST mutation, so
  that boundary has one definition rather than one per caller. It runs:
  * :func:`runtime_placement_gate` — the action-time runtime fingerprint front door
    (R1-F1): the official mutating entry goes zero-write when the active runtime is a
    source/installed skew missing the same-tab placement behavior the repo-local source
    ships (the exact class that split the #13441 lane). Optional / herdr-only.
  * :func:`launcher_compatibility_gate` — the managed-launch launcher must be able to write
    the attestation store and READ the lane's config + the shared lane lifecycle authority
    (Redmine #14258). ``prepare_session`` checks the same conjunction, but only at step 2 —
    after ``git worktree add`` — which is how the measured failure left a worktree and a
    ``rollback_owed`` pair behind. Optional / herdr-only.
* :func:`pair_split_admission` — the operable-pair check (R1-F2): a resolved lane whose
  gateway/worker pair is ``pair_split`` (both panes live but not co-located) is a
  degraded state, not a healthy pair, so it is never adopted / dispatched.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    REASON_LAUNCH_BLOCKED,
    REASON_LAUNCHER_INCOMPATIBLE,
    REASON_MISSING_IDENTITY,
    REASON_PAIR_SPLIT,
    REASON_PARTIAL_PAIR_RECOVERY,
    REASON_RUNTIME_FINGERPRINT,
    REASON_STARTUP_HEALTH_UNCONFIRMED,
    STEP_BLOCKED,
    STEP_EXECUTED,
    ActuationStep,
    SublaneActuationOutcome,
    SublaneStartupObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (  # noqa: E501
    resolve_lane_runtime_root,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
    LAUNCH_CREATE_WORKTREE,
    LAUNCH_SKIP_NO_GIT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.pair_launch_attestation import (  # noqa: E501
    decide_pair_launch_attestation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    SUBLANE_STATE_PAIR_SPLIT,
)


def sender_authority_admission(
    use_case,
    request,
    *,
    dispatch: bool,
    fill_decision: Optional[str],
    fill_override_reason: Optional[str],
):
    """The pre-mutation sender-identity gate for EVERY actuation (#15152 R2/R3).

    This is a WEAK sender-identity signal, NOT caller authentication (ADR-0006,
    #15152 review j#106903/j#106995): the resolved identity comes from the
    process env, which a caller controls, and it is compared to
    repo/board/marker-derivable values — so a caller can present matching values.
    It refuses obviously-wrong or unestablished senders; it does not prove the
    caller IS the coordinator. Forgery-proof caller authentication is deferred to
    #15579. Two refusals, both before any worktree / pane / dispatch side effect:

    - the ops port carries NO ``preflight_dispatch_sender`` capability (#15152 R3,
      review j#106868): absence used to be a #13613 backcompat pass, which made
      every non-herdr backend skip the check — an ops port that cannot even run
      the weak check must not mutate a lane;
    - the capability exists and refuses (#13613): the resolved sender identity
      does not match the workspace anchor / coordinator binding / default lane.

    Returns the typed blocked outcome, or ``None`` to proceed.
    """
    sender_preflight = getattr(use_case.ops, "preflight_dispatch_sender", None)
    if not callable(sender_preflight):
        return use_case._blocked(
            request,
            launch_action=None,
            reason="the actuation ops port provides no sender-authority "
            "capability; an unverifiable sender must not mutate a lane",
            reasons=(
                REASON_MISSING_IDENTITY,
                "sender_attestation",
                "sender_authority_capability_missing",
            ),
            dispatch=dispatch,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
        )
    sender_ok, sender_detail = sender_preflight()
    if not sender_ok:
        return use_case._blocked(
            request,
            launch_action=None,
            reason="dispatch sender attestation failed before actuation; "
            f"{sender_detail}",
            reasons=(REASON_MISSING_IDENTITY, "sender_attestation"),
            dispatch=dispatch,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
        )
    return None


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


def launcher_compatibility_gate(
    use_case,
    request,
    *,
    launch_action,
    dispatch: bool,
    fill_decision,
    fill_override_reason,
) -> Optional[SublaneActuationOutcome]:
    """Managed-launch launcher compatibility gate — BEFORE the worktree (Redmine #14258).

    Optional / herdr-only (the tmux and test ports omit ``preflight_launcher_compatibility``,
    so this is a no-op there). The same conjunction already runs inside ``prepare_session``,
    but that is step 2 of the actuation — *after* ``git worktree add``. The measured failure
    is exactly that shape: the worktree was created, then both slots exited because the
    selected launcher could not read the lane's config, leaving a worktree and a
    ``rollback_owed`` pair (#14258 j#85834). Running it here is what makes the refusal a
    zero-mutation one, the issue's first close condition.

    The gate must know which config the launcher will face, and it derives that from the launch
    decision rather than guessing: only a worktree this run will CREATE has no directory to
    read, so that case reads the committed blob at the lane's base ref (the bytes
    ``git worktree add`` will materialize). A reused worktree — and a non-git lane, which runs
    in the workspace root itself (#13392) and has no ref to read at all — already has the
    directory the wrapper will get as ``--cwd``, so its own file is the target. Returns a
    blocked outcome carrying the typed verdict reason when the launcher is incompatible, else
    ``None``.
    """
    gate = getattr(use_case.ops, "preflight_launcher_compatibility", None)
    if not callable(gate):
        return None
    gate_ok, gate_reason, gate_detail = gate(
        # `pin_base_commit` already replaced this with an immutable full commit SHA for a
        # create, so the config read below addresses the same object `git worktree add` will.
        base_commit=getattr(request, "base_ref", "") or "",
        lane_runtime_root=resolve_lane_runtime_root(
            use_case.ops,
            getattr(request, "worktree_path", "") or "",
            skip_no_git=launch_action == LAUNCH_SKIP_NO_GIT,
        ),
        from_base_ref=launch_action == LAUNCH_CREATE_WORKTREE,
    )
    if gate_ok:
        return None
    return use_case._blocked(
        request,
        launch_action=launch_action,
        reason="managed-launch launcher is incompatible with this lane's target "
        "authorities; refusing before any worktree / process is created — align the "
        f"installed launcher or supply a scoped one, then re-run. {gate_detail}",
        reasons=(REASON_LAUNCHER_INCOMPATIBLE,)
        + ((gate_reason,) if gate_reason else ()),
        dispatch=dispatch,
        fill_decision=fill_decision,
        fill_override_reason=fill_override_reason,
    )


def pin_base_commit(
    use_case,
    request,
    *,
    launch_action,
    dispatch: bool,
    fill_decision,
    fill_override_reason,
):
    """Resolve the lane's base to an immutable commit ONCE — ``(request, outcome)`` (#14258 R1).

    A string ref is not a pin. The launcher preflight reads the config at the lane's base and
    ``git worktree add`` then materializes it, and a branch / remote-tracking ref can advance
    between the two: measured, the preflight admitted a v2 config while the worktree
    materialized a v99 one, defeating the gate whose only purpose is keeping an unverified
    config out of a new worktree (review j#87746 R1).

    So the base is resolved to a full commit SHA here, once, and the returned request carries
    that SHA as its ``base_ref``. Everything downstream — the config read AND the worktree
    creation — then addresses the same immutable object, and a ref that advances afterwards
    changes nothing about what this run materializes.

    Only a worktree this run will CREATE needs it (a reuse / non-git lane reads a directory
    that already exists and passes no ref to git). An unresolvable / ambiguous / non-commit
    base is a zero-mutation refusal, not a fallback to the unpinned ref. Adapters without the
    optional ``resolve_base_commit`` capability (the tmux port, test fakes) are unchanged.
    """
    if launch_action != LAUNCH_CREATE_WORKTREE:
        return request, None
    resolve = getattr(use_case.ops, "resolve_base_commit", None)
    if not callable(resolve):
        return request, None
    requested = (getattr(request, "base_ref", "") or "").strip() or "HEAD"
    pinned = (resolve(requested) or "").strip()
    if not pinned:
        return request, use_case._blocked(
            request,
            launch_action=launch_action,
            reason=f"the lane's base {requested!r} could not be resolved to a single "
            "immutable commit (unknown ref, ambiguous name, or not a commit); refusing "
            "before any worktree so the config that was verified is the config that "
            "materializes",
            reasons=(REASON_LAUNCH_BLOCKED, "base_ref_unpinnable"),
            dispatch=dispatch,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
        )
    return replace(request, base_ref=pinned), None


def pre_mutation_admission(
    use_case,
    request,
    *,
    launch_action,
    dispatch: bool,
    fill_decision,
    fill_override_reason,
):
    """Every admission check that must pass before the FIRST mutation — ``(request, outcome)``.

    One entry point so the "nothing has happened yet" boundary has a single definition and a
    later conjunct cannot be added to one caller only: the base-commit pin (#14258 R1), the
    runtime placement fingerprint (#13705), and the managed-launch launcher compatibility
    conjunction (#14258). Returns the (possibly base-pinned) request and the first blocked
    outcome, or ``None`` when every check passes.

    The pin runs FIRST and its result is what every later check and every mutation sees —
    that ordering is the fix: a gate that verified an unpinned ref would be verifying a
    different object than the one git materializes.
    """
    request, outcome = pin_base_commit(
        use_case,
        request,
        launch_action=launch_action,
        dispatch=dispatch,
        fill_decision=fill_decision,
        fill_override_reason=fill_override_reason,
    )
    if outcome is not None:
        return request, outcome
    for gate in (runtime_placement_gate, launcher_compatibility_gate):
        outcome = gate(
            use_case,
            request,
            launch_action=launch_action,
            dispatch=dispatch,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
        )
        if outcome is not None:
            return request, outcome
    return request, None


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


def startup_health_admission(
    use_case,
    request,
    observation: Optional[SublaneStartupObservation],
    *,
    launch_action,
    dispatch: bool,
    steps: list,
    fill_decision,
    fill_override_reason,
) -> Optional[SublaneActuationOutcome]:
    """Block a non-positive embedded startup before inventory read-back or dispatch.

    ``None`` preserves legacy non-herdr adapters only. Herdr always returns a typed
    observation; any result other than ``ok=True`` is terminal for this actuation. The
    action may owe explicit rollback, but this gate never closes a participant itself.
    """
    if observation is None or observation.ok:
        return None
    rollback_command = (
        "mozyo-bridge herdr session-rollback "
        f"--action-id {observation.action_id}"
        if observation.rollback_owed and observation.action_id
        else None
    )
    steps.append(
        ActuationStep(
            order=2,
            title="confirm embedded startup health",
            status=STEP_BLOCKED,
            detail="session-start did not positively confirm every role "
            f"({observation.health_summary()}); refusing read-back, attestation, "
            "readiness, and dispatch. Explicit rollback remains operator-owned.",
            command=rollback_command,
        )
    )
    return use_case._blocked(
        request,
        launch_action=launch_action,
        reason="embedded session-start health was not positively confirmed; blocked "
        "before post-append read-back and dispatch",
        reasons=(REASON_STARTUP_HEALTH_UNCONFIRMED,),
        dispatch=dispatch,
        steps=tuple(steps),
        startup=observation,
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
    "sender_authority_admission",
    "startup_health_admission",
)
