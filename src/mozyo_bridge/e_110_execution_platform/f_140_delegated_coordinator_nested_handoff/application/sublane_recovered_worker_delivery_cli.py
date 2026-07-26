"""CLI boundary for the recovered managed-pair worker delivery (#14203 R18)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_worker_delivery import (  # noqa: E501
    RecoveredWorkerDeliveryRequest,
)


def cmd_sublane_recover_worker_delivery(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = RecoveredWorkerDeliveryRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        implementation_request_journal=(
            getattr(args, "implementation_request_journal", "") or ""
        ),
        lifecycle_decision_journal=(
            getattr(args, "lifecycle_decision_journal", "") or ""
        ),
        target_action_id=getattr(args, "target_action_id", "") or "",
        retry_of_action_id=getattr(args, "retry_of_action_id", "") or "",
        prior_zero_send_journal=(
            getattr(args, "prior_zero_send_journal", "") or ""
        ),
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovered_worker_delivery_live import (  # noqa: E501
        build_live_recovered_worker_delivery_use_case,
    )

    use_case = build_live_recovered_worker_delivery_use_case(
        repo_root=repo_root,
        env=dict(os.environ),
        issue=request.issue,
        lane=request.lane,
        journal=request.journal,
    )
    outcome = use_case.run(
        request,
        execute=bool(getattr(args, "execute", False)),
    )
    if bool(getattr(args, "json", False)):
        print(
            json.dumps(
                outcome.as_payload(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            f"sublane recover-worker-delivery: {outcome.lane} "
            f"(issue {outcome.issue})\n"
            f"  action_id: {outcome.action_id or '<invalid>'}\n"
            f"  may_deliver: {outcome.may_deliver} "
            f"executed: {outcome.executed} redispatch: {outcome.redispatch}\n"
            f"  {outcome.detail}",
            file=sys.stdout,
        )
    return 1 if outcome.is_blocked else 0


def register_sublane_recovered_worker_delivery_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "recover-worker-delivery",
        help=(
            "Redmine #14203 R18: deliver the unchanged implementation_request "
            "directly to the exact worker of an active recovered pair under a "
            "separate, strict owner-approved action"
        ),
    )
    parser.add_argument("--issue", required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument(
        "--journal",
        required=True,
        help="Owner-approval journal authorizing this exact worker delivery",
    )
    parser.add_argument(
        "--implementation-request-journal",
        dest="implementation_request_journal",
        required=True,
        help="The unchanged implementation_request work anchor",
    )
    parser.add_argument(
        "--lifecycle-decision-journal",
        dest="lifecycle_decision_journal",
        required=True,
        help="The distinct journal that put the current lane generation active",
    )
    parser.add_argument(
        "--target-action-id",
        dest="target_action_id",
        required=True,
        help="Exact pair-recovery startup action bound to both live attestations",
    )
    parser.add_argument(
        "--retry-of-action-id",
        dest="retry_of_action_id",
        required=True,
        help="Canonical id of the proven-zero standard worker-forward attempt",
    )
    parser.add_argument(
        "--prior-zero-send-journal",
        dest="prior_zero_send_journal",
        required=True,
        help="Strict evidence journal proving the standard forward sent nothing",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform one guarded, fenced worker delivery attempt",
    )
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=cmd_sublane_recover_worker_delivery)


__all__ = (
    "cmd_sublane_recover_worker_delivery",
    "register_sublane_recovered_worker_delivery_parser",
)
