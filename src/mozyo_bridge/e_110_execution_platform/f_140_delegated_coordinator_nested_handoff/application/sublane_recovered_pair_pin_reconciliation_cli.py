"""CLI for bounded recovered active-pair pin reconciliation (#14203 R19)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_pair_pin_reconciliation import (  # noqa: E501
    RecoveredPairPinReconciliationRequest,
)


def cmd_sublane_reconcile_recovered_pair_pins(
    args: argparse.Namespace,
) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = RecoveredPairPinReconciliationRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        lifecycle_decision_journal=(
            getattr(args, "lifecycle_decision_journal", "") or ""
        ),
        target_action_id=getattr(args, "target_action_id", "") or "",
        source_revision=int(getattr(args, "source_revision", 0) or 0),
        expected_revision=int(getattr(args, "expected_revision", 0) or 0),
        lane_generation=int(getattr(args, "lane_generation", 0) or 0),
        worktree=str(repo_root),
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovered_pair_pin_reconciliation_live import (  # noqa: E501
        build_live_recovered_pair_pin_reconciliation,
    )

    outcome = build_live_recovered_pair_pin_reconciliation(
        repo_root, env=dict(os.environ)
    ).run(request, execute=bool(getattr(args, "execute", False)))
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
            f"sublane reconcile-recovered-pair-pins: {outcome.lane} "
            f"(issue {outcome.issue})\n"
            f"  ready: {outcome.preflight.ready} executed: {outcome.executed} "
            f"applied: {outcome.applied}\n"
            f"  revision: {outcome.revision or '<unchanged>'}\n"
            f"  {outcome.detail}",
            file=sys.stdout,
        )
    return 1 if outcome.is_blocked else 0


def register_sublane_recovered_pair_pin_reconciliation_parser(
    sublane_sub: Any,
) -> None:
    parser = sublane_sub.add_parser(
        "reconcile-recovered-pair-pins",
        help=(
            "Redmine #14203 R19: replace only the stale declared pair snapshot "
            "of one exact active pair recovered by recover-pair"
        ),
    )
    parser.add_argument("--issue", required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument(
        "--journal",
        required=True,
        help="Exact structured owner-approval journal for this metadata correction",
    )
    parser.add_argument(
        "--lifecycle-decision-journal",
        dest="lifecycle_decision_journal",
        required=True,
    )
    parser.add_argument(
        "--target-action-id",
        dest="target_action_id",
        required=True,
        help="Exact recover-pair action bound to the fresh pair",
    )
    parser.add_argument(
        "--source-revision",
        dest="source_revision",
        required=True,
        type=int,
        help="Hibernated lifecycle revision encoded in the recover-pair action",
    )
    parser.add_argument(
        "--expected-revision",
        dest="expected_revision",
        required=True,
        type=int,
        help="Current active lifecycle revision for the metadata CAS",
    )
    parser.add_argument(
        "--lane-generation",
        dest="lane_generation",
        required=True,
        type=int,
    )
    parser.add_argument("--execute", action="store_true")
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=cmd_sublane_reconcile_recovered_pair_pins)


__all__ = (
    "cmd_sublane_reconcile_recovered_pair_pins",
    "register_sublane_recovered_pair_pin_reconciliation_parser",
)
