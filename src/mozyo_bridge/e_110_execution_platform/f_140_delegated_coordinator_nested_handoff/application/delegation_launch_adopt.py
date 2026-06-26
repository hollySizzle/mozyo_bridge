"""CLI handler for the delegated coordinator launch/adopt primitive (Redmine #12457).

``mozyo-bridge handoff delegate-launch-adopt`` is the runtime entry point for the
parent -> delegated coordinator route. It is **read-only and never sends**: it
runs the same ``agents targets`` discovery pipeline, resolves a deterministic
fail-closed launch/adopt decision with
:func:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_launch_adopt.resolve_launch_adopt`, and
emits

- the decision (mode / outcome / reason / matched candidate summary),
- for an ``adopt`` outcome, the *recommended gated command* the operator runs to
  land the route at the child Codex gateway (a ``handoff send --to codex`` with
  the mandatory ``--target-repo`` identity gate — the actual send still goes
  through ``orchestrate_handoff`` and its cross-lane / cross-session / repo gates,
  which this primitive neither hides nor weakens), and
- the pasteable parent delegation decision + callback target durable record
  (``delegated-coordinator-decision-records.md`` §1 / §4) for the Redmine journal.

The handler holds no routing authority of its own. ``agents targets`` is candidate
discovery only; the decision module enforces the role / repo-identity / lane /
uniqueness filter and the "never a direct child Claude" invariant. The durable
anchor stays the Redmine issue / journal; this command prints a pointer + record.

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
    LaunchAdoptDecision,
    OUTCOME_ADOPT,
    OUTCOME_FAIL_CLOSED,
    OUTCOME_LAUNCH,
    PURPOSE_DELEGATION_PARENT,
    REASON_DELEGATION_DISABLED,
    ROLE_CODEX,
    resolve_launch_adopt,
    validate_callback_targets,
)

#: The fixed receiver role profile the adopted / launched lane coordinator runs.
_DELEGATED_COORDINATOR_ROLE_PROFILE = "delegated_coordinator"

#: Non-zero exit for a fail-closed outcome so a runtime / script can detect "no
#: route formed" without parsing text, while the full decision is still printed
#: (mirrors ``doctor --json`` emitting valid facts on a non-zero exit).
_EXIT_FAIL_CLOSED = 3


def _candidate_from_target(target: object) -> DelegationCandidate:
    """Map an ``agents targets`` ``TargetCandidate`` into a ``DelegationCandidate``.

    Reads only the identity / lane / provenance fields the selector needs; the
    display facts (session / window) ride along for the audit summary. Done in
    the application layer so the domain selector stays decoupled from the
    discovery row type.
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


def _parse_callback_targets(args: argparse.Namespace) -> tuple[CallbackTarget, ...]:
    """Build and validate the callback target set from the CLI flags.

    ``--parent-coordinator-route`` is the mandatory ``delegation_parent`` anchor;
    ``--callback-target purpose=route`` adds optional ``owning_us_coordinator`` /
    ``audit_coordinator`` anchors. Validation (a required delegation parent with a
    non-empty route, known purposes) is delegated to
    :func:`validate_callback_targets`, so the "delegation_parent is always
    required, never omitted by assumption" invariant is enforced in one place.
    """
    targets: list[CallbackTarget] = []
    parent_route = (getattr(args, "parent_coordinator_route", None) or "").strip()
    if parent_route:
        targets.append(
            CallbackTarget(purpose=PURPOSE_DELEGATION_PARENT, route=parent_route)
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
    return validate_callback_targets(targets)


def _recommended_command(
    decision: LaunchAdoptDecision, args: argparse.Namespace
) -> Optional[str]:
    """The gated ``handoff send`` command for an adopt outcome (never auto-run).

    Only an ``adopt`` outcome resolves to a concrete pane, so only it yields a
    runnable command. The command routes to the child Codex gateway with the
    mandatory ``--target-repo`` identity gate and the ``delegated_coordinator``
    role profile — the same gated route ``orchestrate_handoff`` enforces. A
    ``launch`` / ``fail_closed`` outcome has no pane to address and returns
    ``None`` (the operator forms / repairs the lane first).
    """
    if not decision.is_adopt or decision.selected is None:
        return None
    source = getattr(args, "source", None) or "redmine"
    issue = getattr(args, "child_issue", None) or "<child_issue>"
    # Build a flat token list (flag, value, flag, value, ...) and render every
    # token through ``shlex.quote`` so a canonical child repo with spaces (a
    # Google Drive path) or a profile value with shell metacharacters stays a
    # single argv token — otherwise the pasteable command would re-split and the
    # mandatory ``--target-repo`` identity gate would not actually run. Same
    # precedent as ``domain.handoff.explicit_standard_retry_command`` (#12162).
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
        _DELEGATED_COORDINATOR_ROLE_PROFILE,
    ]
    journal = getattr(args, "journal", None)
    if journal:
        parts += ["--journal", journal]
    parent_project = getattr(args, "parent_project", None)
    if parent_project:
        parts += ["--profile-field", f"parent_project={parent_project}"]
    if decision.child_project:
        parts += ["--profile-field", f"child_project={decision.child_project}"]
    return " ".join(shlex.quote(part) for part in parts)


def _render_decision_record(
    decision: LaunchAdoptDecision,
    callback_targets: tuple[CallbackTarget, ...],
    args: argparse.Namespace,
) -> str:
    """Pasteable parent delegation decision + callback targets record.

    Mirrors ``delegated-coordinator-decision-records.md`` §1 (parent delegation
    decision) and §4.1 (delegated callback targets). It is a durable-record
    pointer the operator pastes into the Redmine journal — identity columns and
    anchors only, no absolute path leak (the ``repo_short`` / pane-id projection).
    """
    outcome = decision.outcome
    reason = decision.reason
    if outcome == OUTCOME_ADOPT and decision.selected is not None:
        child_route = (
            f"{decision.selected.pane_id} "
            f"(repo={decision.selected.summary_dict()['repo_short']})"
        )
        child_delegation = "used"
        no_child_reason = "not_applicable"
        correction = "false"
    elif outcome == OUTCOME_LAUNCH:
        child_route = "not_adopted (launch_new)"
        child_delegation = "used"
        no_child_reason = "not_applicable"
        correction = "false"
    elif reason == REASON_DELEGATION_DISABLED:
        child_route = "not_adopted"
        child_delegation = "avoided"
        no_child_reason = "delegation_disabled_by_policy"
        correction = "false"
    else:
        child_route = "unavailable"
        child_delegation = "not_applicable"
        no_child_reason = "not_applicable"
        correction = "true:process_gap_correction_required"

    outcome_field = outcome if reason is None else f"{outcome}:{reason}"
    parent_issue = getattr(args, "parent_issue", None) or "<parent_issue>"
    child_issue = getattr(args, "child_issue", None) or "not_created"
    parent_route = next(
        (t.route for t in callback_targets if t.purpose == PURPOSE_DELEGATION_PARENT),
        "<parent_coordinator_route>",
    )

    lines = [
        "## Parent delegation decision",
        "",
        "- record_kind: parent_delegation_decision",
        f"- parent_coordinator: {parent_route}",
        f"- parent_issue: {parent_issue}",
        f"- child_project: {decision.child_project or '<child_project>'}",
        f"- child_issue: {child_issue}",
        f"- child_coordinator_route: {child_route}",
        f"- child_delegation: {child_delegation}",
        f"- no_child_delegation_reason: {no_child_reason}",
        f"- launch_adopt_mode: {decision.mode}",
        f"- launch_adopt_outcome: {outcome_field}",
        f"- target_repo_gate: required (identity={decision.target_repo_identity})",
        f"- correction_required: {correction}",
        "",
        "## Delegated callback targets",
        "",
        "- record_kind: delegated_callback_targets",
        "- callback_targets:",
    ]
    for target in callback_targets:
        lines.append(f"  - purpose: {target.purpose}")
        lines.append(f"    route: {target.route}")
        lines.append(f"    required: {str(target.required).lower()}")
        lines.append("    outcome_anchor: pending")
    return "\n".join(lines)


def _render_text(
    decision: LaunchAdoptDecision,
    callback_targets: tuple[CallbackTarget, ...],
    recommended: Optional[str],
    args: argparse.Namespace,
) -> str:
    """Compact human-readable decision block plus the durable record."""
    lines = [
        f"launch_adopt_mode: {decision.mode}",
        f"outcome: {decision.outcome}"
        + (f" (reason: {decision.reason})" if decision.reason else ""),
        f"required_role: {decision.required_role}",
        f"target_repo_identity: {decision.target_repo_identity}",
        f"child_project: {decision.child_project or '-'}",
    ]
    if decision.is_adopt and decision.selected is not None:
        sel = decision.selected.summary_dict()
        lines.append(
            f"adopt: pane={sel['pane_id']} workspace={sel['workspace_label']} "
            f"lane={sel['lane_id']} repo={sel['repo_short']}"
        )
    if decision.matched_candidates:
        lines.append(f"matched_candidates ({len(decision.matched_candidates)}):")
        for cand in decision.matched_candidates:
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
    lines.append(_render_decision_record(decision, callback_targets, args))
    return "\n".join(lines)


def cmd_handoff_delegate_launch_adopt(args: argparse.Namespace) -> int:
    """Resolve and print the delegated coordinator launch/adopt decision.

    Read-only over tmux discovery; never sends. Returns ``0`` for an ``adopt`` /
    ``launch`` outcome and :data:`_EXIT_FAIL_CLOSED` for a fail-closed outcome,
    always printing the full decision + durable record first.
    """
    # Lazy import so the parser-build import graph (cli_handoff -> this module)
    # never pulls the heavy ``commands`` module, avoiding any import cycle. ``die``
    # / ``require_tmux`` come from their canonical modules; only the shared
    # discovery pipeline is sourced from ``commands``.
    from mozyo_bridge.application.commands import _agents_target_candidates
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import require_tmux
    from mozyo_bridge.shared.errors import die

    try:
        callback_targets = _parse_callback_targets(args)
    except DelegationLaunchAdoptError as exc:
        die(str(exc))

    require_tmux()
    targets = _agents_target_candidates(args)
    candidates = [_candidate_from_target(t) for t in targets]

    try:
        decision = resolve_launch_adopt(
            mode=args.launch_adopt_mode,
            candidates=candidates,
            target_repo_identity=args.target_repo,
            required_role=ROLE_CODEX,
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
        payload["delivery_record"] = _render_decision_record(
            decision, callback_targets, args
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_render_text(decision, callback_targets, recommended, args))

    return _EXIT_FAIL_CLOSED if decision.outcome == OUTCOME_FAIL_CLOSED else 0


__all__ = ("cmd_handoff_delegate_launch_adopt",)
