"""CLI actuator for the grandchild lane realization stamp (Redmine #12473).

``mozyo-bridge handoff delegate-grandchild-stamp`` is the side-effecting half of
the grandchild dispatch actuator: it takes the **declared** delegation chain
(read from the durable Redmine record, never inferred from pane proximity) and
the realized grandchild lane, validates the tree + the grandchild acceptance
shape through the pure
:func:`mozyo_bridge.domain.grandchild_stamp.resolve_grandchild_stamp_plan`, and
then **stamps** the live ``@mozyo_lane_kind`` / ``@mozyo_delegation_parent``
projection-cache options onto each declared pane so ``agents targets`` /
``delegation_display`` immediately project the grandchild lane with
``KIND=implementation`` / ``DEPTH=2`` / ``PARENT=<delegated coordinator lane>``.

This closes the #12460 gap: the #12458 ``delegate-grandchild-dispatch`` decision
primitive and a same-lane worker handoff stop at a decision record and never
stamp live panes, so the delegated-tree display columns stayed ``-``. This
command connects that decision to the live metadata stamping + a replayable
``## Grandchild lane realization`` durable record.

Safe by default and faithful to the projection-cache posture
(``mozyo_bridge.application.attention_projection`` precedent):

- **Preview unless ``--apply``.** Default prints the exact ``set-option`` plan +
  the realization record and mutates no tmux. ``--apply`` performs the writes
  best-effort (a failed option write is reported, not raised). ``--dry-run``
  forces preview and wins over ``--apply``.
- **Display / audit breadcrumb only.** The stamped options are a re-derivable
  cache; this command holds no routing / approval / close authority and never
  sends. Cross-lane handoff stays bound to the live ``--target-repo`` preflight.
- **Declared, never inferred; never a hidden subagent.** The chain is replayable
  from the durable record, and the realized grandchild lane is a declared,
  durable-anchored, cockpit-visible lane.

The handler body lives here, not in ``application/commands.py``, so the module
stays small and the oversized-``commands.py`` allowlist baseline does not grow
(same convention as ``application/grandchild_dispatch.py``).
"""

from __future__ import annotations

import argparse
import json
from typing import Optional

from mozyo_bridge.domain.grandchild_stamp import (
    DeclaredLane,
    GrandchildStampError,
    GrandchildStampPlan,
    REALIZATION_ADOPT,
    REALIZATIONS,
    resolve_grandchild_stamp_plan,
)

#: Tokens that declare "no parent" (tree root) in a ``--lane`` spec's ``parent=``.
_ROOT_PARENT_TOKENS = frozenset({"", "-", "none", "root"})


def _parse_lane_spec(raw: str) -> DeclaredLane:
    """Parse one ``--lane`` spec into a :class:`DeclaredLane`.

    Spec is comma-separated ``key=value`` pairs:
    ``kind=<lane_kind>,unit=<workspace/lane>,parent=<workspace/lane|->,pane=%N[,pane=%M]``.
    ``pane`` may repeat (one per declared pane); ``parent`` is the direct-parent
    unit pointer or a root token (:data:`_ROOT_PARENT_TOKENS`) for the tree root.
    ``kind`` and ``unit`` are required. Raises :class:`GrandchildStampError` on a
    malformed spec so the caller fails closed with one error type.
    """
    kind: Optional[str] = None
    unit: Optional[str] = None
    parent: Optional[str] = None
    panes: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise GrandchildStampError(
                f"--lane field must be KEY=VALUE; got {token!r} in {raw!r}"
            )
        key, value = token.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "kind":
            kind = value
        elif key == "unit":
            unit = value
        elif key == "parent":
            parent = None if value.lower() in _ROOT_PARENT_TOKENS else value
        elif key == "pane":
            if value:
                panes.append(value)
        else:
            raise GrandchildStampError(
                f"unknown --lane field {key!r}; expected kind/unit/parent/pane"
            )
    if not kind or not unit:
        raise GrandchildStampError(
            f"--lane requires kind= and unit=; got {raw!r}"
        )
    return DeclaredLane(
        unit_id=unit,
        lane_kind=kind,
        delegation_parent=parent,
        panes=tuple(panes),
    )


def _render_realization_record(
    plan: GrandchildStampPlan, args: argparse.Namespace, applied: Optional[bool]
) -> str:
    """Pasteable ``## Grandchild lane realization`` durable record (Redmine #12473).

    Replayable: records the realization mode (launch / adopt), the grandchild
    lane identity + derived depth / parent / root, the adoption reason (adopt
    only), the stamped panes, and the stamp result. ``delegation_depth`` /
    ``delegation_root`` are derived breadcrumbs, not routing keys.
    """
    parent_issue = getattr(args, "parent_issue", None) or "<parent_issue>"
    child_issue = getattr(args, "child_issue", None) or "<child_issue>"
    delegated_coordinator = (
        getattr(args, "delegated_coordinator", None) or plan.grandchild_parent or "-"
    )
    dispatch_anchor = getattr(args, "dispatch_anchor", None) or "pending"
    if applied is None:
        stamp_result = "preview (no tmux mutation)"
    elif applied:
        stamp_result = "applied"
    else:
        stamp_result = "partial (one or more option writes failed; re-run --apply)"

    lines = [
        "## Grandchild lane realization",
        "",
        "- record_kind: grandchild_lane_realization",
        f"- realization: {plan.realization}",
        f"- parent_issue: {parent_issue}",
        f"- child_issue: {child_issue}",
        f"- delegated_coordinator: {delegated_coordinator}",
        f"- grandchild_unit: {plan.grandchild_unit}",
        f"- grandchild_lane_kind: {plan.grandchild_lane_kind}",
        f"- delegation_depth: {plan.grandchild_depth} (derived; hard ceiling 2)",
        f"- delegation_parent: {plan.grandchild_parent or '-'}",
        f"- delegation_root: {plan.grandchild_root}",
        f"- adopt_reason: {plan.adopt_reason or 'not_applicable'}",
        f"- dispatch_anchor: {dispatch_anchor}",
        f"- stamped_panes: {', '.join(plan.stamped_panes) or 'none'}",
        f"- stamped_options: @mozyo_lane_kind, @mozyo_delegation_parent "
        "(the discovery read surface; depth/root are derived, not stamped)",
        f"- stamp_result: {stamp_result}",
        "- projection_note: KIND/DEPTH/PARENT are display/audit breadcrumb only; "
        "never routing authority. No direct cross-lane Claude send; "
        "no hidden subagent.",
    ]
    return "\n".join(lines)


def _render_text(
    plan: GrandchildStampPlan, args: argparse.Namespace, applied: Optional[bool]
) -> str:
    """Compact human-readable stamp block plus the durable realization record."""
    lines = [
        f"realization: {plan.realization}",
        f"grandchild: {plan.grandchild_unit} "
        f"(kind={plan.grandchild_lane_kind} depth={plan.grandchild_depth} "
        f"parent={plan.grandchild_parent or '-'} root={plan.grandchild_root})",
    ]
    if applied is None:
        lines.append("(dry-run) set-option plan (run with --apply to stamp):")
    elif applied:
        lines.append("stamped (applied) set-option plan:")
    else:
        lines.append("warning: partial stamp; set-option plan:")
    for argv in plan.commands:
        lines.append("  tmux " + " ".join(argv))
    if not plan.commands:
        lines.append("  (no panes declared to stamp; derivation-only chain)")
    lines.append("")
    lines.append(_render_realization_record(plan, args, applied))
    return "\n".join(lines)


def cmd_handoff_grandchild_stamp(args: argparse.Namespace) -> int:
    """Stamp live delegation metadata for a grandchild realization (Redmine #12473).

    Builds the pure stamp plan from the declared chain, then previews (default)
    or applies (``--apply``, ``--dry-run`` wins) the ``set-option -p`` writes
    best-effort and prints the replayable ``## Grandchild lane realization``
    record. A plan that cannot be built (invalid tree / shape, or a grandchild
    with no live pane) exits non-zero via ``die``; a built plan returns ``0``.
    """
    from mozyo_bridge.shared.errors import die

    try:
        declared = [_parse_lane_spec(raw) for raw in (getattr(args, "lane", None) or ())]
        plan = resolve_grandchild_stamp_plan(
            declared,
            grandchild_unit=args.grandchild_unit,
            realization=args.realization,
            adopt_reason=getattr(args, "adopt_reason", None),
        )
    except GrandchildStampError as exc:
        die(str(exc))

    # Safe default: preview unless --apply; --dry-run always wins.
    apply = bool(getattr(args, "apply", False)) and not bool(
        getattr(args, "dry_run", False)
    )

    applied: Optional[bool] = None
    if apply:
        from mozyo_bridge.infrastructure.tmux_client import require_tmux, run_tmux

        require_tmux()
        applied = True
        for argv in plan.commands:
            if run_tmux(*argv, check=False).returncode != 0:
                # Best-effort projection cache: a failed option write is recorded,
                # not raised; the run still finishes (attention-project posture).
                applied = False

    if getattr(args, "as_json", False):
        payload = {
            "realization": plan.realization,
            "grandchild_unit": plan.grandchild_unit,
            "grandchild_lane_kind": plan.grandchild_lane_kind,
            "delegation_depth": plan.grandchild_depth,
            "delegation_parent": plan.grandchild_parent,
            "delegation_root": plan.grandchild_root,
            "adopt_reason": plan.adopt_reason,
            "stamped_panes": list(plan.stamped_panes),
            "applied": apply,
            "applied_ok": applied,
            "plan": [list(argv) for argv in plan.commands],
            "projections": [p.as_payload() for p in plan.projections],
            "realization_record": _render_realization_record(plan, args, applied),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_render_text(plan, args, applied))

    return 0


__all__ = ("cmd_handoff_grandchild_stamp",)
