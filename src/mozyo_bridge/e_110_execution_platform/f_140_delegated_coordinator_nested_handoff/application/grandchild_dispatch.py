"""CLI handler for the grandchild dispatch decision primitive (Redmine #12458).

``mozyo-bridge handoff delegate-grandchild-dispatch`` is the runtime entry point
for the delegated coordinator -> grandchild implementation lane route (depth 2).
It is **read-only and never sends**: it resolves the delegation policy gate and a
deterministic fail-closed launch/adopt decision over ``agents targets``
discovery with
:func:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_dispatch.resolve_grandchild_dispatch`, and
emits

- the decision (policy gate / outcome / reason / matched candidate summary),
- for a ``dispatch_adopt`` outcome, the *recommended gated command* the operator
  runs to land the route at the grandchild lane's Codex gateway (a ``handoff
  send --to codex`` with the mandatory ``--target-repo`` identity gate and the
  ``implementation_gateway`` role profile — the actual send still goes through
  ``orchestrate_handoff`` and its cross-lane / cross-session / repo gates, which
  this primitive neither hides nor weakens), and
- the pasteable ``## Delegated dispatch decision`` (decision-records §2) and
  ``## Delegated callback targets`` (§4) durable records for the Redmine journal,
  recording the child -> grandchild dispatch decision *before* any pane
  notification or runtime mutation.

The handler holds no routing authority of its own. ``agents targets`` is
candidate discovery only; the decision module enforces the policy gate, the role
/ repo-identity / lane / uniqueness filter, the "never a direct grandchild
Claude" invariant, and multi-coordinator callback coverage (the GK parent route
and the mozyo_bridge coordinator route are both required and replayable). The
durable anchor stays the Redmine issue / journal; this command prints a pointer +
record. The grandchild lane it decides to launch / adopt is always a declared,
durable-anchored, cockpit-visible lane — never a hidden subagent.

The handler body lives here, not in ``application/commands.py``, so the module is
small and the oversized-``commands.py`` allowlist baseline does not grow.
"""

from __future__ import annotations

import argparse
import json
import shlex
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_launch_adopt import (
    CallbackTarget,
    DelegationCandidate,
    DelegationLaunchAdoptError,
    PURPOSE_AUDIT_COORDINATOR,
    PURPOSE_DELEGATION_PARENT,
    PURPOSE_OWNING_US_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_dispatch import (
    DelegationPolicy,
    GrandchildDispatchDecision,
    OUTCOME_FAIL_CLOSED,
    OWNING_COVERAGE_SAME_AS_PARENT,
    resolve_grandchild_dispatch,
    resolve_no_dispatch,
    validate_grandchild_callback_targets,
)

#: The role profile the dispatched grandchild lane's Codex gateway runs (it is the
#: target-lane Codex that routes same-lane to the implementation_worker Claude).
_GRANDCHILD_GATEWAY_ROLE_PROFILE = "implementation_gateway"

#: Non-zero exit for a fail-closed outcome so a runtime / script can detect "no
#: grandchild route formed" without parsing text, while the full decision is
#: still printed (mirrors ``doctor --json`` emitting valid facts on a non-zero
#: exit, and the #12457 ``delegate-launch-adopt`` exit convention).
_EXIT_FAIL_CLOSED = 3


def _candidate_from_target(target: object) -> DelegationCandidate:
    """Map an ``agents targets`` ``TargetCandidate`` into a ``DelegationCandidate``.

    Reads only the identity / lane / provenance fields the selector needs; the
    display facts (session / window) ride along for the audit summary. Identical
    projection to the #12457 ``delegate-launch-adopt`` handler so the grandchild
    selection sees the same shape.
    """
    return DelegationCandidate(
        pane_id=getattr(target, "pane_id", "") or "",
        role=getattr(target, "role", "") or "",
        repo_root=getattr(target, "repo_root", None),
        workspace_id=getattr(target, "workspace_id", None),
        workspace_label=getattr(target, "workspace_label", None),
        lane_id=getattr(target, "lane_id", "") or "",
        lane_label=getattr(target, "lane_label", None),
        confidence=getattr(target, "confidence", "") or "",
        ambiguous=bool(getattr(target, "ambiguous", False)),
        session=getattr(target, "session", "") or "",
        window_name=getattr(target, "window_name", "") or "",
    )


def _policy_from_args(args: argparse.Namespace) -> DelegationPolicy:
    """Build the :class:`DelegationPolicy` from the durable-record CLI flags.

    The policy values come from the durable record (the same way #12457 reads
    ``--launch-adopt-mode``); the ``.mozyo-bridge/config.yaml`` ``delegation:``
    loader is the #12390 follow-up. Out-of-range / malformed values are not
    rejected here — the domain :func:`effective_policy` clamps them fail-closed
    and surfaces a diagnostic, so a bad policy suppresses delegation rather than
    crashing.
    """
    return DelegationPolicy(
        enable_delegated_coordinator=bool(
            getattr(args, "enable_delegated_coordinator", False)
        ),
        enable_grandchild_dispatch=bool(
            getattr(args, "enable_grandchild_dispatch", False)
        ),
        max_delegation_depth=int(getattr(args, "max_delegation_depth", 1)),
        max_active_child_lanes=int(getattr(args, "max_active_child_lanes", 1)),
        decision_record_policy=getattr(args, "decision_record_policy", "minimal"),
    )


def _parse_callback_targets(args: argparse.Namespace) -> tuple[CallbackTarget, ...]:
    """Build and validate the grandchild callback target set from the CLI flags.

    ``--parent-coordinator-route`` is the mandatory ``delegation_parent`` anchor
    (the GK parent coordinator route). ``--owning-coordinator-route`` adds the
    required ``owning_us_coordinator`` anchor (the mozyo_bridge coordinator route);
    ``--callback-target purpose=route`` adds further optional anchors;
    ``--owning-same-as-parent`` declares the owning/audit coverage is the same
    route as the parent. Multi-coordinator coverage validation (both routes
    replayable, never omitted by assumption) is delegated to
    :func:`validate_grandchild_callback_targets`.
    """
    targets: list[CallbackTarget] = []
    parent_route = (getattr(args, "parent_coordinator_route", None) or "").strip()
    if parent_route:
        targets.append(
            CallbackTarget(purpose=PURPOSE_DELEGATION_PARENT, route=parent_route)
        )
    owning_route = (getattr(args, "owning_coordinator_route", None) or "").strip()
    if owning_route:
        targets.append(
            CallbackTarget(
                purpose=PURPOSE_OWNING_US_COORDINATOR, route=owning_route, required=True
            )
        )
    for raw in getattr(args, "callback_target", None) or ():
        if "=" not in raw:
            raise DelegationLaunchAdoptError(
                f"--callback-target must be PURPOSE=ROUTE; got {raw!r}"
            )
        purpose, route = raw.split("=", 1)
        targets.append(
            CallbackTarget(
                purpose=purpose.strip(), route=route.strip(), required=False
            )
        )
    owning_coverage = (
        OWNING_COVERAGE_SAME_AS_PARENT
        if getattr(args, "owning_same_as_parent", False)
        else None
    )
    return validate_grandchild_callback_targets(targets, owning_coverage=owning_coverage)


def _recommended_command(
    decision: GrandchildDispatchDecision, args: argparse.Namespace
) -> Optional[str]:
    """The gated ``handoff send`` command for a ``dispatch_adopt`` outcome.

    Only an adopt outcome resolves to a concrete grandchild Codex gateway pane, so
    only it yields a runnable command. The command routes to that gateway with the
    mandatory ``--target-repo`` identity gate and the ``implementation_gateway``
    role profile — the same gated route ``orchestrate_handoff`` enforces. Every
    token is rendered through :func:`shlex.quote` so a canonical child repo with
    spaces or a profile value with shell metacharacters stays a single argv token
    (same precedent as ``domain.handoff.explicit_standard_retry_command`` /
    #12162, and the #12457 fix at #12457 j#63794). A ``dispatch_launch`` /
    ``no_dispatch`` / ``fail_closed`` outcome has no pane to address and returns
    ``None``.
    """
    if not decision.is_adopt or decision.selected is None:
        return None
    source = getattr(args, "source", None) or "redmine"
    issue = getattr(args, "child_issue", None) or "<child_issue>"
    parts = [
        "mozyo-bridge",
        "handoff",
        "send",
        "--to",
        "codex",
        "--target",
        decision.selected.pane_id,
        "--target-repo",
        str(decision.target_repo_identity),
        "--source",
        source,
        "--issue",
        issue,
        "--kind",
        "implementation_request",
        "--role-profile",
        _GRANDCHILD_GATEWAY_ROLE_PROFILE,
    ]
    journal = getattr(args, "journal", None)
    if journal:
        parts += ["--journal", journal]
    return " ".join(shlex.quote(part) for part in parts)


def _grandchild_lane_pointer(decision: GrandchildDispatchDecision) -> str:
    """The grandchild lane identity pointer for the decision record (no path leak)."""
    if decision.is_adopt and decision.selected is not None:
        sel = decision.selected.summary_dict()
        return f"{decision.selected.pane_id} (repo={sel['repo_short']}, lane={sel['lane_id']})"
    if decision.is_launch:
        return "to_launch (new visible cockpit lane)"
    if decision.is_no_dispatch:
        return "not_applicable"
    return "unavailable"


def _render_dispatch_decision_record(
    decision: GrandchildDispatchDecision, args: argparse.Namespace
) -> str:
    """Pasteable ``## Delegated dispatch decision`` record (decision-records §2).

    Records the delegation-identifying fields the parent coordinator inspects on
    callback / audit: the delegated coordinator pointer, the parent route, the
    parent / child issues, the grandchild lane identity, the dispatch anchor, the
    audit-safe ``delegation_depth``, and the ``grandchild_dispatch`` / ``purpose``
    / ``no_dispatch_reason`` fields. The operator continues with the spine
    ``## Sublane dispatch decision`` bandwidth fields below this block.
    """
    if decision.is_adopt:
        grandchild_dispatch = "dispatched"
    elif decision.is_launch:
        grandchild_dispatch = "dispatched"
    elif decision.is_no_dispatch:
        grandchild_dispatch = "avoided"
    else:
        grandchild_dispatch = "not_applicable"

    outcome_field = (
        decision.outcome
        if decision.reason is None
        else f"{decision.outcome}:{decision.reason}"
    )
    parent_issue = getattr(args, "parent_issue", None) or "<parent_issue>"
    child_issue = getattr(args, "child_issue", None) or "<child_issue>"
    delegated_coordinator = (
        getattr(args, "delegated_coordinator", None) or "<delegated_coordinator_lane>"
    )
    parent_route = (
        getattr(args, "parent_coordinator_route", None) or "<parent_coordinator_route>"
    )
    dispatch_anchor = getattr(args, "dispatch_anchor", None) or "pending"
    eff = decision.policy_gate.effective

    lines = [
        "## Delegated dispatch decision",
        "",
        "- record_kind: delegated_dispatch_decision",
        f"- delegated_coordinator: {delegated_coordinator}",
        f"- parent_coordinator_route: {parent_route}",
        f"- parent_issue: {parent_issue}",
        f"- child_issue: {child_issue}",
        f"- grandchild_lane: {_grandchild_lane_pointer(decision)}",
        f"- dispatch_anchor: {dispatch_anchor}",
        f"- delegation_depth: {decision.delegation_depth} (shallow; hard ceiling 2)",
        f"- grandchild_dispatch: {grandchild_dispatch}",
        f"- purpose: {decision.purpose}",
        f"- no_dispatch_reason: {decision.no_dispatch_reason or 'not_applicable'}",
        f"- launch_adopt_outcome: {outcome_field}",
        f"- target_repo_gate: required (identity={decision.target_repo_identity})",
        f"- visible_lane_required: {str(decision.visible_lane_required).lower()} "
        f"(declared durable-anchored lane, never a hidden subagent)",
        f"- policy_permitted: {str(decision.policy_gate.permitted).lower()}"
        + (
            f" (gate_reason: {decision.policy_gate.reason})"
            if decision.policy_gate.reason
            else ""
        ),
        f"- effective_policy: enable_delegated_coordinator="
        f"{str(eff.enable_delegated_coordinator).lower()} "
        f"enable_grandchild_dispatch={str(eff.enable_grandchild_dispatch).lower()} "
        f"max_delegation_depth={eff.effective_max_depth} "
        f"max_active_child_lanes={eff.effective_max_active_child_lanes} "
        f"decision_record_policy={eff.decision_record_policy}",
    ]
    if eff.diagnostics:
        lines.append(f"- policy_diagnostics: {', '.join(eff.diagnostics)}")
    return "\n".join(lines)


def _render_callback_targets_record(
    callback_targets: tuple[CallbackTarget, ...], args: argparse.Namespace
) -> str:
    """Pasteable ``## Delegated callback targets`` record (decision-records §4.1).

    Lists the purpose-tagged callback routes whose outcomes must be recorded
    before the delegated callback is complete. The GK parent route
    (``delegation_parent``) and the mozyo_bridge coordinator route
    (``owning_us_coordinator`` / ``audit_coordinator``) are both present and
    required — the multi-coordinator coverage the #12458 acceptance requires.
    """
    parent_issue = getattr(args, "parent_issue", None) or "<parent_issue>"
    child_issue = getattr(args, "child_issue", None) or "<child_issue>"
    has_owning = any(
        t.purpose in (PURPOSE_OWNING_US_COORDINATOR, PURPOSE_AUDIT_COORDINATOR)
        for t in callback_targets
    )
    pass_condition = "all_required_callback_outcomes_recorded"
    lines = [
        "## Delegated callback targets",
        "",
        "- record_kind: delegated_callback_targets",
        "- source_state: pending (dispatch decision; gate state set on callback)",
        f"- parent_issue: {parent_issue}",
        f"- child_issue: {child_issue}",
        f"- owning_coverage: "
        + (
            "distinct_owning_or_audit_target"
            if has_owning
            else OWNING_COVERAGE_SAME_AS_PARENT
        ),
        "- callback_targets:",
    ]
    for target in callback_targets:
        lines.append(f"  - purpose: {target.purpose}")
        lines.append(f"    route: {target.route}")
        lines.append(f"    required: {str(target.required).lower()}")
        lines.append("    outcome_anchor: pending")
    if not has_owning:
        lines.append("  - purpose: owning_us_coordinator")
        lines.append("    route: same_as_delegation_parent")
        lines.append("    required: true")
        lines.append("    outcome_anchor: pending")
    lines.append(f"- pass_condition: {pass_condition}")
    return "\n".join(lines)


def _render_text(
    decision: GrandchildDispatchDecision,
    callback_targets: tuple[CallbackTarget, ...],
    recommended: Optional[str],
    args: argparse.Namespace,
) -> str:
    """Compact human-readable decision block plus the durable records."""
    gate = decision.policy_gate
    lines = [
        f"outcome: {decision.outcome}"
        + (f" (reason: {decision.reason})" if decision.reason else ""),
        f"delegation_depth: {decision.delegation_depth} (hard ceiling 2)",
        f"policy_permitted: {gate.permitted}"
        + (f" (gate_reason: {gate.reason})" if gate.reason else ""),
        f"purpose: {decision.purpose}",
        f"target_repo_identity: {decision.target_repo_identity}",
        f"child_project: {decision.child_project or '-'}",
        f"visible_lane_required: {decision.visible_lane_required} "
        f"(never a hidden subagent)",
    ]
    if decision.no_dispatch_reason:
        lines.append(f"no_dispatch_reason: {decision.no_dispatch_reason}")
    la = decision.launch_adopt
    if decision.is_adopt and decision.selected is not None:
        sel = decision.selected.summary_dict()
        lines.append(
            f"adopt: pane={sel['pane_id']} workspace={sel['workspace_label']} "
            f"lane={sel['lane_id']} repo={sel['repo_short']}"
        )
    if la is not None and la.matched_candidates:
        lines.append(f"matched_candidates ({len(la.matched_candidates)}):")
        for cand in la.matched_candidates:
            c = cand.summary_dict()
            lines.append(
                f"  - {c['pane_id']}\t{c['role']}\tworkspace={c['workspace_label']}"
                f"\tlane={c['lane_id']}\trepo={c['repo_short']}"
                f"\tconf={c['confidence']}\tambiguous={c['ambiguous']}"
            )
    if recommended:
        lines.append("")
        lines.append("recommended (run manually; this command is the only send):")
        lines.append(f"  {recommended}")
    lines.append("")
    lines.append(_render_dispatch_decision_record(decision, args))
    lines.append("")
    lines.append(_render_callback_targets_record(callback_targets, args))
    return "\n".join(lines)


def cmd_handoff_grandchild_dispatch(args: argparse.Namespace) -> int:
    """Resolve and print the grandchild (depth-2) dispatch decision.

    Read-only over tmux discovery; never sends. Returns ``0`` for a
    ``dispatch_adopt`` / ``dispatch_launch`` / ``no_dispatch`` outcome and
    :data:`_EXIT_FAIL_CLOSED` for a fail-closed outcome, always printing the full
    decision + durable records first. The durable child -> grandchild dispatch
    decision is recorded before any pane notification or runtime mutation.
    """
    # Lazy import so the parser-build import graph never pulls the heavy
    # ``commands`` module, avoiding an import cycle (same pattern as the #12457
    # ``delegate-launch-adopt`` handler). ``die`` / ``require_tmux`` come from
    # their canonical modules; only the shared discovery pipeline is from
    # ``commands``.
    from mozyo_bridge.shared.errors import die

    try:
        callback_targets = _parse_callback_targets(args)
    except DelegationLaunchAdoptError as exc:
        die(str(exc))

    policy = _policy_from_args(args)
    no_dispatch_reason = getattr(args, "no_dispatch", None)

    if no_dispatch_reason:
        # No-dispatch (grandchild_dispatch: avoided) needs no tmux discovery: the
        # delegated coordinator keeps the work in its own lane.
        try:
            decision = resolve_no_dispatch(
                policy=policy,
                no_dispatch_reason=no_dispatch_reason,
                current_depth=int(getattr(args, "current_depth", 1)),
                child_project=getattr(args, "child_project", None),
            )
        except DelegationLaunchAdoptError as exc:
            die(str(exc))
        recommended = None
    else:
        from mozyo_bridge.application.commands import _agents_target_candidates
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import require_tmux

        require_tmux()
        targets = _agents_target_candidates(args)
        candidates = [_candidate_from_target(t) for t in targets]

        try:
            decision = resolve_grandchild_dispatch(
                policy=policy,
                mode=args.launch_adopt_mode,
                candidates=candidates,
                target_repo_identity=args.target_repo,
                current_depth=int(getattr(args, "current_depth", 1)),
                active_grandchild_lanes=int(getattr(args, "active_grandchild_lanes", 0)),
                excluded_lane_ids=tuple(getattr(args, "excluded_lane", None) or ()),
                child_project=getattr(args, "child_project", None),
            )
        except DelegationLaunchAdoptError as exc:
            die(str(exc))
        recommended = _recommended_command(decision, args)

    if getattr(args, "as_json", False):
        payload = decision.to_dict()
        payload["callback_targets"] = [t.to_dict() for t in callback_targets]
        payload["recommended_command"] = recommended
        payload["dispatch_decision_record"] = _render_dispatch_decision_record(
            decision, args
        )
        payload["callback_targets_record"] = _render_callback_targets_record(
            callback_targets, args
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_render_text(decision, callback_targets, recommended, args))

    return _EXIT_FAIL_CLOSED if decision.outcome == OUTCOME_FAIL_CLOSED else 0


__all__ = ("cmd_handoff_grandchild_dispatch",)
