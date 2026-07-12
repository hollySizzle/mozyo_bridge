"""CLI actuator for the grandchild lane realization stamp (Redmine #12473).

``mozyo-bridge handoff delegate-grandchild-stamp`` is the side-effecting half of
the grandchild dispatch actuator: it takes the **declared** delegation chain
(read from the durable Redmine record, never inferred from pane proximity) and
the realized grandchild lane, validates the tree + the grandchild acceptance
shape through the pure
:func:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp.resolve_grandchild_stamp_plan`, and
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
(``mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.application.attention_projection`` precedent):

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

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (
    DeclaredLane,
    GrandchildBinding,
    GrandchildStampError,
    GrandchildStampPlan,
    GrandchildTargetIdentity,
    REALIZATION_ADOPT,
    REALIZATIONS,
    RealizationGateResult,
    evaluate_grandchild_realization_gate,
    redact_unit_token,
    resolve_realized_grandchild_binding,
    resolve_grandchild_stamp_plan,
)

#: Non-zero exit when the realization gate blocks (grandchild required but not
#: realized), so the delegated-coordinator runtime can detect "do not proceed to
#: a same-lane worker handoff" without parsing text, while the replayable gate
#: record is still printed (same convention as the #12458 dispatch fail-closed
#: exit). ``blocked`` is a valid recorded outcome, not a caller error, so it is
#: returned rather than raised through ``die``.
_EXIT_GATE_BLOCKED = 3

#: Tokens that declare "no parent" (tree root) in a ``--lane`` spec's ``parent=``.
_ROOT_PARENT_TOKENS = frozenset({"", "-", "none", "root"})


def _canonical_repo_identity(path: Optional[str]) -> Optional[str]:
    """Canonicalize a repo path to the SAME identity the ``--target-repo`` gate uses.

    Repo identity must be compared on one canonical form so a `.` / `..` /
    ``~`` / NFC-vs-NFD spelling of the same checkout never reads as a mismatch
    (Redmine #13571 j#75473 F3). Reuses the shared workspace-identity primitives:
    :func:`mozyo_bridge.shared.paths.resolve_repo_root`
    (``Path.expanduser().resolve()``) followed by
    :func:`mozyo_bridge.shared.paths.normalize_path_unicode` (the fixed NFD form),
    the same canonicalization the live discovery repo root goes through. Returns
    ``None`` for an empty path (the binding then fails closed on the mandatory
    repo re-match). Best-effort: an unresolvable path falls back to its
    Unicode-normalized raw form rather than raising.
    """
    from mozyo_bridge.shared.paths import normalize_path_unicode, resolve_repo_root

    if not path or not str(path).strip():
        return None
    raw = str(path).strip()
    try:
        resolved = str(resolve_repo_root(raw))
    except (OSError, RuntimeError, ValueError):
        resolved = raw
    return normalize_path_unicode(resolved)


def _parse_lane_spec(raw: str) -> DeclaredLane:
    """Parse one ``--lane`` spec into a :class:`DeclaredLane`.

    Spec is comma-separated ``key=value`` pairs:
    ``kind=<lane_kind>,unit=<workspace/lane>,parent=<workspace/lane|->,pane=%N[,pane=%M]``.
    ``pane`` may repeat (one per declared pane); ``parent`` is the direct-parent
    unit pointer or a root token (:data:`_ROOT_PARENT_TOKENS`) for the tree root.
    ``kind`` and ``unit`` are required. Raises :class:`GrandchildStampError` on a
    malformed spec so the caller fails closed with one error type. The error text
    never echoes the raw ``--lane`` value / token, only the sanitized grammar —
    an operator-supplied value may be a private path, and this runs before the
    domain stable-unit validator, so a raw echo would leak it (Redmine #13571
    j#75565 R9-F1).
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
                "each --lane field must be written KEY=VALUE (comma-separated "
                "kind=/unit=/parent=/pane= pairs)"
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
            # Echo the key only when it is a plain identifier (a likely typo such
            # as `kidn=`); a path-like key is redacted so it cannot leak.
            safe_key = key if key.isidentifier() else "<redacted>"
            raise GrandchildStampError(
                f"unknown --lane field {safe_key!r}; expected kind/unit/parent/pane"
            )
    if not kind or not unit:
        raise GrandchildStampError(
            "each --lane requires both kind= and unit="
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
    # The record-only `--delegated-coordinator` is operator-supplied and is not
    # validated by the plan; project it through the shared unit redactor so a
    # path-like override cannot leak into the pasteable record (Redmine #13571
    # R8-F1). `plan.grandchild_parent` is already validated stable by the plan.
    delegated_raw = getattr(args, "delegated_coordinator", None)
    delegated_coordinator = (
        redact_unit_token(delegated_raw)
        if delegated_raw
        else (plan.grandchild_parent or "-")
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
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import require_tmux, run_tmux

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


def _discover_delegation_units(args: argparse.Namespace):
    """Discover live lanes and re-resolve each delegation-tree unit's identity.

    Returns a list of
    :class:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp.InventoryUnit`,
    one per ``<workspace_id>/<lane_id>`` unit, ready for
    :func:`resolve_realized_grandchild_binding`. Beyond the display breadcrumb and
    the per-lane canonical repo, each unit carries the two live-resolution facts
    the display columns cannot express (Redmine #13571 / #12454 j#75444 F2):

    - ``has_codex_gateway``: a live ``codex`` gateway pane exists for the unit, so
      the lane is route-bound (a Claude-only remnant is not).
    - ``ambiguous``: the folded unit carried conflicting candidate panes
      (disagreeing repo / KIND / depth / parent) or a weakly-identified candidate,
      so the raw ambiguity is preserved instead of being silently collapsed by the
      per-unit fold.

    Reads ``agents targets`` discovery; the delegation derivation is the same
    read-only #12466 projection ``agents targets`` itself uses.
    """
    from mozyo_bridge.application.commands import _agents_target_candidates
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import CONFIDENCE_STRONG
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import derive_targets_delegation
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import GRANDCHILD_GATEWAY_ROLE, InventoryUnit
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import require_tmux

    require_tmux()
    candidates = _agents_target_candidates(args)
    displays = derive_targets_delegation(candidates)

    # Group candidate panes by their folded unit id, keeping every pane's facts so
    # conflicting panes surface as ambiguity rather than a silent first-writer win.
    grouped: dict[str, list] = {}
    for cand in candidates:
        display = displays.get(cand.pane_id)
        if display is None:
            continue
        unit_id = f"{getattr(cand, 'workspace_id', '') or ''}/{getattr(cand, 'lane_id', '') or ''}"
        grouped.setdefault(unit_id, []).append((cand, display))

    result: list[InventoryUnit] = []
    for unit_id, members in grouped.items():
        # Resolve the route-bound Codex gateway (the discovery projection uses
        # `role`, not `agent_kind`). It is route-bound only when the unit has
        # EXACTLY ONE `role==codex` candidate AND that sole candidate is strong /
        # non-ambiguous. Two codex candidates (even strong+weak), zero, a weak or
        # ambiguous sole candidate all fail the "single trusted gateway" rule
        # (Redmine #13571 j#75473 F1 / j#75480 F1).
        codex_members = [
            cand for cand, _ in members if getattr(cand, "role", None) == GRANDCHILD_GATEWAY_ROLE
        ]
        strong_sole_codex = (
            codex_members[0]
            if len(codex_members) == 1
            and getattr(codex_members[0], "confidence", None) == CONFIDENCE_STRONG
            and not getattr(codex_members[0], "ambiguous", False)
            else None
        )
        has_codex_gateway = strong_sole_codex is not None
        # The gateway's OWN repo is authoritative — never synthesized from a
        # sibling Claude pane's repo. Missing gateway repo -> None -> the
        # binding's mandatory repo re-match fails closed.
        gateway_repo = (
            _canonical_repo_identity(getattr(strong_sole_codex, "repo_root", None))
            if has_codex_gateway
            else None
        )
        # Canonical non-empty repos across ALL panes of the unit: a checkout
        # disagreement (codex on repo-A, Claude on repo-B) is a conflicted
        # identity, not a trusted single realization (j#75480 F2).
        member_repos = {
            _canonical_repo_identity(getattr(cand, "repo_root", None))
            for cand, _ in members
            if _canonical_repo_identity(getattr(cand, "repo_root", None)) is not None
        }
        weak_candidate = any(getattr(cand, "ambiguous", False) for cand, _ in members)
        derived = [d for _, d in members if d.status == "derived"]
        # Authoritative KIND/depth/parent come from the derived breadcrumb(s); if
        # the derived panes disagree, the unit is ambiguous.
        derived_facts = {
            (d.lane_kind, d.delegation_depth, d.delegation_parent) for d in derived
        }
        if derived:
            lane_kind, depth, parent = next(
                (d.lane_kind, d.delegation_depth, d.delegation_parent) for d in derived
            )
            status = "derived"
        else:
            first = members[0][1]
            lane_kind, depth, parent, status = (
                first.lane_kind,
                first.delegation_depth,
                first.delegation_parent,
                first.status,
            )
        ambiguous = (
            weak_candidate
            or len(codex_members) > 1
            or len(member_repos) > 1
            or len(derived_facts) > 1
        )
        result.append(
            InventoryUnit(
                unit_id=unit_id,
                lane_kind=lane_kind,
                delegation_depth=depth,
                delegation_parent=parent,
                status=status,
                repo_identity=gateway_repo,
                has_codex_gateway=has_codex_gateway,
                ambiguous=ambiguous,
            )
        )
    return result


def _render_gate_record(
    result: RealizationGateResult,
    args: argparse.Namespace,
    binding: Optional[GrandchildBinding] = None,
) -> str:
    """Pasteable ``## Grandchild realization gate`` record (Redmine #12474 / j#64151)."""
    # Redact a malformed (e.g. path-like) coordinator context so no private path
    # reaches the pasteable record (Redmine #13571 R7-F2).
    delegated_raw = getattr(args, "delegated_coordinator_unit", None)
    delegated = redact_unit_token(delegated_raw) if delegated_raw else "<delegated_coordinator_unit>"
    parent_issue = getattr(args, "parent_issue", None) or "<parent_issue>"
    child_issue = getattr(args, "child_issue", None) or "<child_issue>"
    # Redact a malformed (e.g. absolute-path typo) unit so a private host path
    # never reaches the pasteable gate record (Redmine #13571 R6-F1).
    target_unit = redact_unit_token(getattr(args, "grandchild_unit", None))
    lines = [
        "## Grandchild realization gate",
        "",
        "- record_kind: grandchild_realization_gate",
        f"- verdict: {result.verdict}",
        f"- grandchild_required: {str(result.grandchild_required).lower()}",
        f"- parent_issue: {parent_issue}",
        f"- child_issue: {child_issue}",
        f"- delegated_coordinator_unit: {delegated}",
        f"- dispatch_selected_grandchild_unit: {target_unit}",
        f"- realized_grandchild_unit: {result.realized_grandchild_unit or 'none'}",
        f"- reason: {result.reason}",
    ]
    if binding is not None:
        lines.append(f"- identity_binding: {binding.outcome} ({binding.reason})")
    lines.append(
        "- enforcement: the gate binds to the EXACT dispatch-selected grandchild "
        "identity (workspace/lane/role/repo/parent/depth) and fails closed on a "
        "missing/mismatched/ambiguous identity; a same-lane worker handoff alone "
        "does not satisfy display acceptance (#13571 / #12460 / #12474 j#64151)."
    )
    if result.is_blocked:
        lines.append(
            "- remediation: create/adopt a route-bound grandchild lane/window, run "
            "`handoff delegate-grandchild-stamp`, then re-run this gate; or record "
            "blocked replayably. Do not proceed to a same-lane worker handoff as "
            "if it were a display PASS."
        )
    lines.append(
        "- projection_note: KIND/DEPTH/PARENT are display/audit breadcrumb only; "
        "never routing authority. No direct cross-lane Claude send; no hidden subagent."
    )
    return "\n".join(lines)


def cmd_handoff_grandchild_gate(args: argparse.Namespace) -> int:
    """Gate a delegated-coordinator worker handoff on grandchild realization (#12473 j#64151).

    Reads ``agents targets`` discovery, derives each lane's delegation breadcrumb,
    looks for a route-bound depth-2 ``implementation`` grandchild lane under
    ``--delegated-coordinator-unit``, and evaluates the realize-or-blocked gate.
    Prints the replayable ``## Grandchild realization gate`` record and returns
    :data:`_EXIT_GATE_BLOCKED` when the gate blocks (a grandchild is required but
    none is realized) so the runtime records blocked replayably instead of
    silently treating a same-lane worker handoff as display acceptance. Returns
    ``0`` for a ``realized`` or ``same_lane_ok`` verdict.
    """
    units = _discover_delegation_units(args)
    # Bind to the EXACT dispatch-selected/created/adopted grandchild identity
    # (never the first depth-2 sibling), then re-verify it against the live
    # inventory (Redmine #13571 / #12454 j#75444 F1).
    gc_unit = (getattr(args, "grandchild_unit", None) or "").strip()
    target = (
        GrandchildTargetIdentity(
            unit_id=gc_unit,
            delegation_parent=args.delegated_coordinator_unit,
            # Canonicalize the operator-supplied repo to the same identity the
            # live gateway repo is compared on (F3), so spelling never mismatches.
            repo_identity=_canonical_repo_identity(getattr(args, "grandchild_repo", None)),
        )
        if gc_unit
        else None
    )
    binding = resolve_realized_grandchild_binding(
        units, target=target, delegated_coordinator_unit=args.delegated_coordinator_unit
    )
    result = evaluate_grandchild_realization_gate(
        grandchild_required=bool(getattr(args, "require_grandchild", True)),
        realized_grandchild_unit=binding.matched_unit,
    )

    if getattr(args, "as_json", False):
        payload = {
            "verdict": result.verdict,
            "grandchild_required": result.grandchild_required,
            "delegated_coordinator_unit": redact_unit_token(args.delegated_coordinator_unit),
            "dispatch_selected_grandchild_unit": (
                redact_unit_token(gc_unit) if gc_unit else None
            ),
            "realized_grandchild_unit": result.realized_grandchild_unit,
            "identity_binding": {
                "outcome": binding.outcome,
                "matched_unit": binding.matched_unit,
                "reason": binding.reason,
            },
            "reason": result.reason,
            "blocked": result.is_blocked,
            "gate_record": _render_gate_record(result, args, binding),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"verdict: {result.verdict}")
        print(f"reason: {result.reason}")
        print("")
        print(_render_gate_record(result, args, binding))

    return _EXIT_GATE_BLOCKED if result.is_blocked else 0


__all__ = ("cmd_handoff_grandchild_stamp", "cmd_handoff_grandchild_gate")
