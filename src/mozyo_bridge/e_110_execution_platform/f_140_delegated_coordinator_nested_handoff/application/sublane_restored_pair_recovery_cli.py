"""CLI for owner-approved post-reboot active-pair replacement (Redmine #15227)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery import (  # noqa: E501
    RestoredPairRecoveryRequest,
    SublaneRestoredPairRecoveryUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery_live import (  # noqa: E501
    build_live_restored_pair_recovery_ops,
)


def format_restored_pair_recovery_text(outcome) -> str:
    plan = outcome.plan
    lines = [
        f"sublane recover-restored-pair: {outcome.lane} (issue {outcome.issue})",
        f"  status: {outcome.status}  executed: {outcome.executed}",
        f"  may_recover: {plan.may_recover}",
        f"  action_id: {plan.action_id}",
        f"  action_generation: {plan.action_generation}",
        f"  worktree_authority: {plan.worktree_authority_reason}",
    ]
    for slot in plan.slots:
        lines.append(
            f"  {slot.slot_role}: provider={slot.provider or '-'} "
            f"locator={slot.locator or '-'} revision={slot.revision or '-'} "
            f"cwd_matches={slot.cwd_matches} attestation={slot.attestation_state} "
            f"runtime_state={slot.runtime_state} settled={slot.runtime_settled}"
        )
    if plan.blocked_reasons:
        lines.append("  blocked: " + ", ".join(plan.blocked_reasons))
    if outcome.required_approval_marker:
        lines.append("  required_approval_marker: " + outcome.required_approval_marker)
    lines.append(
        "  conversation_resume_guaranteed: "
        + str(outcome.conversation_resume_guaranteed).lower()
    )
    if outcome.detail:
        lines.append("  detail: " + outcome.detail)
    return "\n".join(lines)


def cmd_sublane_recover_restored_pair(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = RestoredPairRecoveryRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        action_id=getattr(args, "action_id", "") or "",
        action_generation=int(getattr(args, "action_generation", 0) or 0),
        allow_pending_composer_loss=bool(
            getattr(args, "allow_pending_composer_loss", False)
        ),
        gateway_assigned_name=getattr(args, "gateway_assigned_name", "") or "",
        gateway_locator=getattr(args, "gateway_locator", "") or "",
        gateway_revision=getattr(args, "gateway_revision", "") or "",
        worker_assigned_name=getattr(args, "worker_assigned_name", "") or "",
        worker_locator=getattr(args, "worker_locator", "") or "",
        worker_revision=getattr(args, "worker_revision", "") or "",
    )
    use_case = SublaneRestoredPairRecoveryUseCase(
        build_live_restored_pair_recovery_ops(repo_root)
    )
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_restored_pair_recovery_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_recover_restored_pair_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "recover-restored-pair",
        help=(
            "Redmine #15227: replace the exact idle gateway+worker generations of an "
            "active lane when reboot restoration left their command-shell CWD or startup "
            "identity proof inconsistent. Preserves the worktree/branch; default is read-only."
        ),
    )
    parser.add_argument("--issue", required=True, help="Redmine issue owned by the active lane")
    parser.add_argument("--lane", required=True, help="Exact active lane id / branch")
    parser.add_argument(
        "--journal",
        default="",
        help="Exact structured direct-owner approval journal (required by --execute)",
    )
    parser.add_argument(
        "--action-id",
        default="",
        help="Exact action_id printed by preflight (required by --execute)",
    )
    parser.add_argument(
        "--action-generation",
        type=int,
        default=0,
        help="Exact positive generation printed by preflight (required by --execute)",
    )
    parser.add_argument(
        "--allow-pending-composer-loss",
        action="store_true",
        help=(
            "Owner accepts that unsent composer text in these exact old panes may be lost. "
            "Required even for an approval-ready preflight; files/worktree are not discarded."
        ),
    )
    for flag, dest, label in (
        ("--gateway-assigned-name", "gateway_assigned_name", "gateway assigned name"),
        ("--gateway-locator", "gateway_locator", "gateway old locator"),
        ("--gateway-revision", "gateway_revision", "gateway inventory revision"),
        ("--worker-assigned-name", "worker_assigned_name", "worker assigned name"),
        ("--worker-locator", "worker_locator", "worker old locator"),
        ("--worker-revision", "worker_revision", "worker inventory revision"),
    ):
        parser.add_argument(
            flag,
            dest=dest,
            default="",
            help=(
                f"Exact {label} printed by preflight. Optional on the first run; supply it "
                "when replaying a partially completed action."
            ),
        )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the exact approved close/relaunch/attestation transaction",
    )
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_recover_restored_pair)


__all__ = (
    "cmd_sublane_recover_restored_pair",
    "format_restored_pair_recovery_text",
    "register_sublane_recover_restored_pair_parser",
)
