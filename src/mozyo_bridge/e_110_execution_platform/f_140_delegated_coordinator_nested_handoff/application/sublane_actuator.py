"""`mozyo-bridge sublane start --execute` live actuator boundary (Redmine #12973).

#12955 shipped ``sublane create`` / ``start`` as *planning only* — it emits the fail-closed,
replayable worktree + gateway pane + worker pane + dispatch plan but actuates nothing. This
module adds the **creation-side live actuator** that executes that plan in one action, wiring
the choreography a coordinator otherwise hand-assembles for every max-5 sublane:

1. create (or reuse) the lane git worktree / branch — the additive #12604
   :meth:`LiveSublaneGitOperations.create_worktree`, already inside the
   ``worktree-lifecycle-boundary.md`` boundary;
2. append a cockpit-visible Codex gateway + Claude worker column, binding the lane / role /
   workspace / repo-root stamps (or **adopt** an already-live lane, never clobbering it);
3. confirm the lane is visible with both panes and the identity stamps (fail-closed
   read-back);
4. dispatch the governed ``implementation_request`` to the gateway pane (coordinator ->
   sublane Codex gateway -> same-lane Claude worker), with the durable Redmine journal as
   the anchor.

Design decision (issue scope — "``workflow step`` に統合するか ``sublane start --dispatch`` を
内部 primitive とするか"): the live actuator is delivered as an **opt-in** ``sublane start
--execute`` path and kept as the internal primitive. ``sublane start`` without ``--execute``
remains the #12955 plan-only surface byte-for-byte (back-compat), so the standard,
side-effect-free UX is unchanged; ``--execute`` is the one-action live path and
``--no-dispatch`` stops after pane creation. The #12755 ``workflow step`` standard entrypoint
can later wrap this primitive without re-deriving the choreography. Rationale: #12955 already
owns the ``sublane`` surface and lane-identity vocabulary, ``workflow step`` is a not-yet-
implemented higher-level state machine, and the boundary-doc runbook already sequences
git-worktree-add -> cockpit-append -> dispatch as discrete steps.

Boundary: this actuator is **creation-side / additive only**. It never removes a worktree,
kills a pane, deletes a branch, or attempts a merge — the destructive retire half stays gated
behind a Design Consultation (``worktree-lifecycle-boundary.md``) and is untouched
(:func:`decide_retire_integration` / ``sublane retire`` still preflight-only). It self-
authorizes no close / carve-out / owner decision, and it dispatches only to the Codex gateway
rail (never a cross-lane Claude direct send, never a hidden subagent). Raw ``%pane`` typing is
never a normal-UX surface: pane ids are runtime evidence resolved from the inventory.

OOP-first boundary (mirrors #12933 ``launch_command`` / #12955 ``sublane_lifecycle_command``):
:class:`SublaneActuateUseCase` holds the fail-closed decision flow and never touches IO; the
:class:`SublaneActuatorOps` port owns every side effect; :class:`LiveSublaneActuatorOps`
composes the real primitives (the #12604 git ops + the ``cockpit append`` / ``handoff send``
CLI contract the coordinator already drives by hand); a typed
:class:`~...domain.sublane_actuation.SublaneActuationOutcome` carries the machine-readable,
replayable payload; and the thin ``cmd_sublane_start`` handler owns stdout and the exit code.

Module layout (#13299 module-health decomposition — byte-preserving facade split): the
injected IO port + live adapter live in :mod:`.sublane_actuator_ops`; the fail-closed
:class:`SublaneActuateUseCase` decision flow lives in :mod:`.sublane_actuator_use_case`;
this module keeps the pure text renderer + the thin ``cmd_sublane_start`` CLI handler and
re-exports the carved names, so the public import surface is unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (  # noqa: E501
    DEFAULT_GATEWAY_READY_INTERVAL_SECONDS,
    DEFAULT_GATEWAY_READY_PROBES,
    GATEWAY_READY_CAPTURE_LINES,
    LiveSublaneActuatorOps,
    SublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
    SublaneActuateUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
    cmd_sublane_create,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    DISPATCH_DELIVERY_NOT_SENT,
    DISPATCH_DELIVERY_UNCERTAIN,
    DISPATCH_GATEWAY_NOTIFIED,
    SublaneActuationOutcome,
    render_actuation_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    FillDecisionInputs,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    SublaneCreateRequest,
    redact_worktree_paths,
)


# ---------------------------------------------------------------------------
# Text rendering (pure).
# ---------------------------------------------------------------------------


def format_actuate_text(outcome: SublaneActuationOutcome) -> str:
    header = "sublane start"
    if not outcome.execute:
        header += " (dry-run)"
    lines = [f"{header}: {outcome.status}", f"  reason: {outcome.reason}"]
    if outcome.launch_action:
        lines.append(f"  launch_action: {outcome.launch_action}")
    if outcome.gateway_pane or outcome.worker_pane:
        lines.append(
            f"  lane: gateway={outcome.gateway_pane or '-'} "
            f"worker={outcome.worker_pane or '-'} "
            f"adopted={str(outcome.adopted).lower()}"
        )
    if outcome.gateway_ready is not None:
        lines.append(f"  gateway_ready: {str(outcome.gateway_ready).lower()}")
        if outcome.gateway_ready is False:
            lines.append(
                "  ! gateway readiness probe was NOT confirmed before the governed "
                "send; the dispatch result below is independently authoritative"
            )
    if outcome.dispatch_target:
        lines.append(
            f"  dispatch: {outcome.dispatch_result} -> {outcome.dispatch_target}"
        )
        if outcome.dispatch_result == DISPATCH_GATEWAY_NOTIFIED:
            lines.append(
                "  ! gateway notified only; worker dispatch NOT confirmed "
                "(worker_dispatch_confirmed=false). Drive the ack with "
                "`sublane dispatch-worker --execute` (#12988); if no worker "
                "progress lands, classify with `sublane callback-recovery "
                "--dispatch-delivered`"
            )
        elif outcome.dispatch_result == DISPATCH_DELIVERY_UNCERTAIN:
            lines.append(
                "  ! delivery uncertain: body and/or Enter may have reached the "
                "gateway; blind retry is prohibited. Read the receiver and durable "
                "anchor before any explicit recovery"
            )
        elif outcome.dispatch_result == DISPATCH_DELIVERY_NOT_SENT:
            lines.append(
                "  ! delivery did not reach the gateway; the structured "
                "pre-injection refusal permits retry after its cause is fixed"
            )
    if outcome.is_blocked:
        lines.append("  -> blocked: " + ", ".join(outcome.blocked_reasons))
    else:
        for step in outcome.steps:
            lines.append(f"  {step.order}. [{step.status}] {step.title}: {step.detail}")
            if step.command:
                lines.append(f"       $ {step.command}")
    lines.append("  durable record:")
    for jline in render_actuation_journal(outcome).splitlines():
        lines.append(f"    {jline}")
    # #13368: the full text output is pasteable; redact the host-local absolute
    # worktree path (e.g. inside the replayable `git worktree add` / `cockpit append
    # --repo` command lines) to its portable sibling basename. The exact command with
    # the absolute path is still available in the `--json` payload for local replay.
    return redact_worktree_paths("\n".join(lines), outcome.worktree_path)


# ---------------------------------------------------------------------------
# Thin CLI handler.
# ---------------------------------------------------------------------------


def _repo_root(args: argparse.Namespace) -> Path:
    repo = getattr(args, "repo", None)
    return Path(repo).expanduser() if repo else Path.cwd()


def resolve_dispatch_admission_args(
    args: argparse.Namespace,
) -> tuple[Optional[FillDecisionInputs], Optional[str]]:
    """Bind the #13290 dispatch-admission flags to (fill_inputs, override_reason).

    Shared by ``sublane create`` and ``sublane dispatch-worker`` so the gate arms
    identically on both live dispatch paths. ``--lane`` values are already parsed to
    :class:`LaneState` by the argparse ``type`` at registration time.

    The gate is **caller-armed**: it returns ``fill_inputs = None`` (gate not armed,
    dispatch proceeds unchanged) unless the coordinator declared fill context — any
    ``--lane`` / non-zero count / ``--owner-or-release-gate`` / ``--override-fill-stop``.
    An override reason alone arms the gate (against the all-defaults
    ``stop_no_ready_work``) so an explicit override is always a deliberate, recorded
    act rather than a silent no-op.
    """
    lanes = tuple(getattr(args, "lane", None) or ())
    ready_independent = int(getattr(args, "ready_independent", 0) or 0)
    ready_overlap = int(getattr(args, "ready_overlap", 0) or 0)
    capacity = int(getattr(args, "capacity", 0) or 0)
    owner_gate = bool(getattr(args, "owner_or_release_gate", False))
    override = (getattr(args, "override_fill_stop", None) or "").strip() or None

    armed = bool(
        lanes or ready_independent or ready_overlap or capacity or owner_gate or override
    )
    if not armed:
        return None, override

    fill_inputs = FillDecisionInputs(
        lanes=lanes,
        ready_independent_work=ready_independent,
        ready_overlapping_work=ready_overlap,
        capacity_remaining=capacity,
        owner_or_release_gate_active=owner_gate,
    )
    return fill_inputs, override


def _resolve_sublane_ops(
    args: Optional[argparse.Namespace],
    *,
    repo_root: Path,
    request: SublaneCreateRequest,
    quiet_stdout: bool,
) -> SublaneActuatorOps:
    """Pick the creation-side actuation adapter for the configured terminal backend.

    ``backend: herdr`` (Redmine #13331) → the per-lane-workspace
    :class:`~...application.sublane_actuator_herdr_ops.HerdrSublaneActuatorOps`, carrying the
    requested lane identity so its inventory read-back projects the lane. Anything else →
    the tmux :class:`LiveSublaneActuatorOps`, unchanged.

    ``args`` is not read by the selection; the typed #15152 service passes ``None``
    while the historical test callers keep passing a Namespace. Kept as the ONE
    selection point for both entries.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_backend_is_herdr,
    )

    if repo_backend_is_herdr(repo_root):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
            HerdrSublaneActuatorOps,
        )

        return HerdrSublaneActuatorOps(
            repo_root=repo_root,
            lane_label=request.lane_label,
            issue=request.issue,
            branch=request.branch,
            # Redmine #13681 W1: the durable-anchor journal that authorizes this lane's
            # owner binding rides the same `--journal` the dispatch leg carries. A create
            # with no journal is owner-unbound (no lifecycle row).
            journal=request.journal or "",
            # Redmine #13647 T1b: the creating caller's delegation-geometry assertion
            # travels to the create-time lifecycle declaration, where it is stored
            # generation-bound as the lane's offline heal authority for pane placement.
            lane_kind=request.lane_kind,
            quiet_stdout=quiet_stdout,
        )
    return LiveSublaneActuatorOps(repo_root=repo_root, quiet_stdout=quiet_stdout)


def _sublane_start_provider_preflight_blocked(repo_root, *, snapshot=None) -> bool:
    """Fail closed (True) when a lane's bound gateway/worker provider cannot be launched.

    Redmine #13569 R1-F2 / R2-F2b. Since #15152 the decision itself lives in the
    typed shared service (:func:`...sublane_start_service.provider_preflight_refusal`),
    which the CLI and the MCP mutating entry both run; this wrapper keeps the
    historical print-and-bool shape for its direct callers and prints the same
    fixed wording to stderr.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service import (  # noqa: E501
        provider_preflight_refusal,
    )

    refusal = provider_preflight_refusal(Path(str(repo_root)), snapshot=snapshot)
    if refusal is None:
        return False
    print(refusal.message, file=sys.stderr)
    return True


def cmd_sublane_start(args: argparse.Namespace) -> int:
    """``sublane create`` / ``start`` dispatcher.

    With neither ``--execute`` nor ``--dry-run`` this is the #12955 plan-only surface
    (delegates byte-for-byte to :func:`cmd_sublane_create`, back-compat). ``--dry-run``
    previews the one-action actuation plan with no side effect (and wins over ``--execute``
    when both are given); ``--execute`` runs the live actuator.
    """
    execute = bool(getattr(args, "execute", False))
    dry_run = bool(getattr(args, "dry_run", False))
    if not execute and not dry_run:
        return cmd_sublane_create(args)

    repo_root = _repo_root(args)
    fill_inputs, override_fill_stop = resolve_dispatch_admission_args(args)
    # #13293: in --json mode, confine the composed sub-CLI (cockpit append / handoff
    # send) progress text to stderr so stdout is a single parseable JSON envelope.
    json_mode = bool(getattr(args, "json", False))

    # Redmine #15152: the whole actuation body — the #13002 work-unit config
    # fail-closed, the #15146 delegated_coordinator parent-authority admission,
    # the #13569 provider launchability preflight, the backend-selected adapter,
    # and the use-case run — lives in the typed shared service the local MCP
    # `sublane_start` tool also calls. This adapter only terminates the parsed
    # Namespace and owns stdout/stderr + the exit code, so the two entries cannot
    # drift (#14224); the Namespace never crosses into the service.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service import (  # noqa: E501
        SublaneStartCommand,
        run_sublane_start,
    )

    result = run_sublane_start(
        SublaneStartCommand(
            repo_root=repo_root,
            issue=getattr(args, "issue", "") or "",
            lane_label=getattr(args, "lane_label", "") or "",
            branch=getattr(args, "branch", "") or "",
            worktree_path=getattr(args, "worktree", "") or "",
            journal=getattr(args, "journal", None),
            upstream_coordinator=getattr(args, "upstream_coordinator", None),
            work_unit=getattr(args, "work_unit", None),
            work_unit_decision_anchor=getattr(
                args, "work_unit_decision_journal", None
            ),
            # Redmine #14224 correction (j#84882): both request-construction
            # sites (the plan-only surface and this actuating one) must carry
            # the same leaf admission input, or the same argv resolves
            # differently across `sublane create` (plan) and `--dry-run` /
            # `--execute`.
            leaf_standalone=bool(getattr(args, "leaf_standalone", False)),
            base_ref=getattr(args, "base_ref", None),
            lane_kind=getattr(args, "lane_kind", "") or "",
            execute=execute and not dry_run,
            dispatch=not getattr(args, "no_dispatch", False),
            target_repo=getattr(args, "target_repo", None) or "auto",
            gateway_ready_timeout=float(
                getattr(args, "gateway_ready_timeout", 10.0) or 0.0
            ),
            fill_inputs=fill_inputs,
            override_fill_stop=override_fill_stop,
            quiet_stdout=json_mode,
            provider_snapshot=getattr(args, "snapshot", None),
        )
    )
    if result.refused:
        print(result.refusal.message, file=sys.stderr)
        return result.exit_code
    outcome = result.outcome
    if json_mode:
        print(
            json.dumps(
                outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        print(format_actuate_text(outcome))
    return result.exit_code


__all__ = (
    "DEFAULT_GATEWAY_READY_PROBES",
    "DEFAULT_GATEWAY_READY_INTERVAL_SECONDS",
    "GATEWAY_READY_CAPTURE_LINES",
    "SublaneActuatorOps",
    "LiveSublaneActuatorOps",
    "SublaneActuateUseCase",
    "resolve_dispatch_admission_args",
    "format_actuate_text",
    "cmd_sublane_start",
)
