"""``sublane rebind-restored-pair`` command surface (Redmine #15656)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind import (  # noqa: E501
    SublaneRestoredPairRebindUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind_live import (  # noqa: E501
    LiveRestoredPairRebindOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
    RestoredPairRebindRequest,
)


def format_restored_pair_rebind_text(outcome) -> str:
    plan = outcome.plan
    lines = [
        f"sublane rebind-restored-pair: {outcome.lane} (issue {outcome.issue})",
        f"  status: {outcome.status}  executed: {outcome.executed}  "
        f"applied: {outcome.applied}",
        f"  may_rebind: {plan.may_rebind}",
        f"  lane_disposition: {plan.lane_disposition or '-'}",
        f"  revision: {plan.revision}  lane_generation: {plan.lane_generation}",
    ]
    for slot in (plan.gateway, plan.worker):
        if slot is None:
            continue
        lines.append(
            f"  {slot.slot_role}: provider={slot.provider or '-'} "
            f"assigned_name={slot.assigned_name or '-'} "
            f"declared_locator={slot.declared_locator or '-'} "
            f"live_locator={slot.live_locator or '-'} "
            f"attestation={slot.attestation_state or '-'} ready={slot.ready}"
            + (" skipped=True" if slot.skipped else "")
        )
        if slot.generation_state:
            lines.append(f"    generation: {slot.generation_state}")
        if slot.reason:
            lines.append(f"    reason: {slot.reason}")
    for entry in plan.reattest_lineage:
        lines.append(
            f"  reattest_lineage[{entry.get('slot_role', '-')}]: "
            f"terminal {entry.get('old_terminal_id', '-')} -> "
            f"{entry.get('new_terminal_id', '-')}, "
            f"locator {entry.get('old_locator', '-')} -> "
            f"{entry.get('new_locator', '-')}, "
            f"participant_repin={entry.get('participant_locator_repin')}"
        )
    if plan.blocked_reasons:
        lines.append("  blocked: " + ", ".join(plan.blocked_reasons))
    if outcome.detail:
        lines.append("  detail: " + outcome.detail)
    if outcome.revision is not None and outcome.executed:
        lines.append(f"  result_revision: {outcome.revision}")
    return "\n".join(lines)


def cmd_sublane_rebind_restored_pair(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = RestoredPairRebindRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        allow_single_slot=bool(getattr(args, "allow_single_slot", False)),
    )
    use_case = SublaneRestoredPairRebindUseCase(
        LiveRestoredPairRebindOps(repo_root=repo_root)
    )
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if bool(getattr(args, "json", False)):
        print(
            json.dumps(
                outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        print(format_restored_pair_rebind_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_rebind_restored_pair_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "rebind-restored-pair",
        help=(
            "Redmine #15656: CAS-replace an ACTIVE lane's stale declared_slots pair "
            "snapshot when a herdr server restart restored the SAME attested "
            "gateway+worker sessions onto new pane locators. Redmine #15769: when a "
            "restored slot's server-owned terminal id (and possibly its locator) is "
            "new while the launch-generation row still records the launch-time "
            "values, additionally CAS re-attest that row (and the startup-transaction "
            "participant locator when the pane moved) from server-owned inventory "
            "facts, recording the old->new lineage in the outcome. Default is a "
            "read-only preflight; --execute writes only the lifecycle pin snapshot / "
            "generation row / startup participant (locator + re-minted pane_bound_v2 "
            "receipt) / attestation record (never a close / launch / send / "
            "worktree change; lane_generation is unchanged)."
        ),
    )
    parser.add_argument(
        "--issue", required=True, help="Redmine issue owned by the active lane"
    )
    parser.add_argument("--lane", required=True, help="Exact active lane id / branch")
    parser.add_argument(
        "--journal",
        default="",
        help=(
            "Optional Redmine journal id the operator records this rebind under "
            "(carried into the outcome payload; the rail's authority is the live "
            "restart evidence, not this anchor)"
        ),
    )
    parser.add_argument(
        "--allow-single-slot",
        action="store_true",
        help=(
            "Redmine #15769: resolve a restored slot even when the pair's OTHER "
            "slot has no live named row at all; the missing slot is reported as "
            "the typed fact missing_live_slot and its declared pin stays "
            "byte-unchanged. Every gate on the resolved slot is unchanged."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Perform the re-attest CAS writes (declared slots, generation rows, "
            "startup participant locator + receipt, attestation record) when every "
            "fail-closed gate passes. Default: read-only preflight, zero-write."
        ),
    )
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_rebind_restored_pair)


__all__ = (
    "cmd_sublane_rebind_restored_pair",
    "format_restored_pair_rebind_text",
    "register_sublane_rebind_restored_pair_parser",
)
