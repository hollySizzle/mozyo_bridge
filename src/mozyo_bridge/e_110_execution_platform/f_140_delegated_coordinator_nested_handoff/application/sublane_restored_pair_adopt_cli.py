"""``sublane adopt-restored-pair`` command surface (Redmine #15811)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_adopt import (  # noqa: E501
    SublaneRestoredPairAdoptUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_adopt_live import (  # noqa: E501
    LiveRestoredPairAdoptOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_adopt import (  # noqa: E501
    RestoredPairAdoptRequest,
)


def format_restored_pair_adopt_text(outcome) -> str:
    plan = outcome.plan
    lines = [
        f"sublane adopt-restored-pair: {outcome.lane} (issue {outcome.issue})",
        f"  status: {outcome.status}  executed: {outcome.executed}  "
        f"applied: {outcome.applied}",
        f"  may_adopt: {plan.may_adopt}",
        f"  lane_disposition: {plan.lane_disposition or '-'}",
        f"  revision: {plan.revision}  lane_generation: {plan.lane_generation}",
    ]
    for slot in (plan.gateway, plan.worker):
        if slot is None:
            continue
        lines.append(
            f"  {slot.slot_role}: provider={slot.provider or '-'} "
            f"assigned_name={slot.assigned_name or '-'} "
            f"live_locator={slot.live_locator or '-'} "
            f"attestation={slot.attestation_state or '-'} ready={slot.ready}"
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


def cmd_sublane_adopt_restored_pair(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = RestoredPairAdoptRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
    )
    use_case = SublaneRestoredPairAdoptUseCase(
        LiveRestoredPairAdoptOps(repo_root=repo_root)
    )
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if bool(getattr(args, "json", False)):
        print(
            json.dumps(
                outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        print(format_restored_pair_adopt_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_adopt_restored_pair_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "adopt-restored-pair",
        help=(
            "Redmine #15811: declare an ACTIVE lane's declared_slots pair for the FIRST "
            "time when a herdr server generation change restored its gateway+worker pair "
            "but the row never had pins (the create-path shape, declared_pins_absent) — "
            "the case rebind-restored-pair refuses as declared_slots_unresolved and "
            "`sublane create` adopt refuses as unattested_slot. Subject is ONLY a row "
            "whose pin snapshot is exactly absent; any non-empty snapshot is refused so a "
            "degraded one is never overwritten. Requires the full server-owned proof chain "
            "(unique live decoded slot, attested launch-generation row, startup-participant "
            "lineage, matching self-attestation) and additionally re-attests the "
            "generation / participant / attestation records so the unchanged read-side "
            "verifiers pass again. Default is a read-only preflight; --execute writes only "
            "the lifecycle pin snapshot / generation row / startup participant / "
            "attestation record (never a close / launch / send / worktree change; "
            "lane_generation is unchanged)."
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
            "Optional Redmine journal id the operator records this declaration under "
            "(carried into the outcome payload; the rail's authority is the live restore "
            "evidence, not this anchor)"
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Perform the writes (startup participant locator + receipt, attestation "
            "record, launch-generation row, then the empty-only declared-slots CAS) when "
            "every fail-closed gate passes. Default: read-only preflight, zero-write."
        ),
    )
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_adopt_restored_pair)


__all__ = (
    "cmd_sublane_adopt_restored_pair",
    "format_restored_pair_adopt_text",
    "register_sublane_adopt_restored_pair_parser",
)
