"""CLI for previewing and applying one managed Herdr pair placement (#14608)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mozyo_bridge.core.state.workspace_registry import (
    load_workspace_by_id,
    load_workspace_by_path,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    safe_text,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_live_pair_placement import (
    PlacementApplyResult,
    PlacementPlan,
    production_live_pair_placement,
)


def _workspace_id(args: argparse.Namespace) -> str:
    requested = str(getattr(args, "workspace", "") or "").strip()
    repo = Path(getattr(args, "repo", None) or ".").resolve()
    by_path = load_workspace_by_path(repo)
    if requested:
        by_id = load_workspace_by_id(requested)
        if by_id is None:
            raise ValueError("--workspace is not registered")
        if getattr(args, "repo", None) and (
            by_path is None or by_path.workspace_id != requested
        ):
            raise ValueError("--workspace and --repo do not identify the same workspace")
        return requested
    if by_path is None:
        raise ValueError("the selected repository is not registered")
    return by_path.workspace_id


def _plan_text(plan: PlacementPlan) -> str:
    lines = [
        f"Herdr pair placement: {safe_text(plan.status)}",
        f"unit: workspace={safe_text(plan.workspace_id)} "
        f"lane={safe_text(plan.lane_id)}",
        f"reason: {safe_text(plan.reason)}",
        f"detail: {safe_text(plan.detail)}",
    ]
    if plan.target is not None:
        lines.extend(
            (
                f"current: split={safe_text(plan.current_split)} "
                f"order={','.join(safe_text(value) for value in plan.current_order)} "
                f"ratio={plan.current_ratio:g}",
                f"target: split={safe_text(plan.target.split)} "
                f"order={','.join(safe_text(value) for value in plan.target.order)} "
                f"ratio={plan.target.ratio:g}",
                "operations: "
                + (", ".join(plan.operations) if plan.operations else "none"),
            )
        )
    return "\n".join(lines)


def _apply_text(result: PlacementApplyResult) -> str:
    lines = [
        f"Herdr pair placement apply: {safe_text(result.status)}",
        f"reason: {safe_text(result.reason)}",
        f"detail: {safe_text(result.detail)}",
    ]
    if result.recovery:
        lines.append(f"recovery: {safe_text(result.recovery)}")
    lines.extend(("final:", _plan_text(result.after)))
    return "\n".join(lines)


def cmd_herdr_pair_placement_preview(args: argparse.Namespace) -> int:
    try:
        workspace_id = _workspace_id(args)
        plan = production_live_pair_placement().preview(workspace_id, args.lane)
    except Exception:
        print("error: Herdr pair placement preview could not resolve its runtime inputs")
        return 1
    if getattr(args, "json", False):
        print(json.dumps(plan.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        print(_plan_text(plan))
    return 0 if plan.ok else 1


def cmd_herdr_pair_placement_apply(args: argparse.Namespace) -> int:
    try:
        workspace_id = _workspace_id(args)
        result = production_live_pair_placement().apply(workspace_id, args.lane)
    except Exception:
        print("error: Herdr pair placement apply could not resolve its runtime inputs")
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        print(_apply_text(result))
    return 0 if result.ok else 1


def register_herdr_pair_placement_parser(herdr_sub, *, add_repo_option) -> None:
    parser = herdr_sub.add_parser(
        "pair-placement",
        help=(
            "Preview or apply effective placement to a dedicated two-pane Herdr "
            "unit without pane-id input or process restart."
        ),
        description=(
            "Resolve the Unit by registered workspace and lane identity, verify both "
            "current launch generations and dedicated two-pane geometry, then preview "
            "or explicitly apply split/order/ratio changes."
        ),
    )
    sub = parser.add_subparsers(dest="pair_placement_command", required=True)
    commands = (
        (
            "preview",
            cmd_herdr_pair_placement_preview,
            "Show current and target placement without changing panes.",
        ),
        (
            "apply",
            cmd_herdr_pair_placement_apply,
            "Recheck and apply the placement, then measure it again.",
        ),
    )
    for name, handler, help_text in commands:
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--workspace",
            default="",
            help=(
                "Registered workspace id (default: resolve from --repo/current "
                "directory)."
            ),
        )
        command.add_argument(
            "--lane", default="default", help="Exact lane id (default: default)."
        )
        command.add_argument(
            "--json", action="store_true", help="Emit structured JSON."
        )
        add_repo_option(command)
        command.set_defaults(func=handler)


__all__ = (
    "cmd_herdr_pair_placement_apply",
    "cmd_herdr_pair_placement_preview",
    "register_herdr_pair_placement_parser",
)
