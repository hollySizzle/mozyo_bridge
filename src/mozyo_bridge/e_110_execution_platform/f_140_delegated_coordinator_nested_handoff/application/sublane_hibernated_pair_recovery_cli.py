"""``mozyo-bridge sublane recover-pair`` CLI surface (Redmine #13847).

The operator-facing wiring for the hibernated exact-pair recovery use case
(:mod:`...sublane_hibernated_pair_recovery`) — argument parsing, the live composition root
lookup, and the text / JSON rendering. Split out of the use-case module under the codebase's
``*_cli.py`` convention so the recovery contract and its command surface stay separately
readable (and the use-case module stays within the module-health line without an allowlist
entry, Redmine #14475).

Behaviour is unchanged: this is a verbatim move of the rendering + command + parser
registration, importing the vocabulary and the use case from the module that owns them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    BLOCK_IDENTITY_INCOMPLETE,
    RecoverPairDeliveryRetryRequest,
    RecoverPairOutcome,
    RecoverPairRequest,
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
    REDISPATCH_SKIPPED,
    REDISPATCH_TARGET_RETIRING,
    REDISPATCH_UNCERTAIN,
)

def format_recover_pair_text(outcome: RecoverPairOutcome) -> str:
    lines = [
        f"sublane recover-pair: {outcome.lane} (issue {outcome.issue})",
        f"  may_recover: {outcome.preflight.may_recover} executed: {outcome.executed}",
    ]
    for slot in outcome.preflight.slots:
        lines.append(f"  {slot.role}: {slot.disposition} (recovers={slot.recovers})")
    # Review j#88563 F3 / j#88571 F3: printed on EVERY path with a STABLE shape — including a
    # preflight-blocked run, which previously showed neither line. An operator scanning the
    # output must not have to infer "the field is missing, so presumably nothing".
    lines.append(f"  applied: {', '.join(outcome.effects) or 'nothing'}")
    lines.append(f"  unresolved: {', '.join(outcome.unresolved) or 'none'}")
    if outcome.is_blocked:
        lines.append(
            "  -> fail-closed blocked: " + ", ".join(outcome.preflight.blocked_reasons or (outcome.detail,))
        )
        if outcome.resume is not None and outcome.resume.is_blocked:
            lines.append("  resume: " + ", ".join(outcome.resume.preflight.blocked_reasons))
        if outcome.relaunch_reason:
            lines.append(f"  relaunch_reason: {outcome.relaunch_reason}")
        if outcome.relaunch_startup is not None:
            lines.append(
                "  startup: "
                f"{outcome.relaunch_startup.health_summary()} "
                f"rollback_owed={outcome.relaunch_startup.rollback_owed}"
            )
        if outcome.rollback_pointer:
            lines.append(f"  rollback_pointer: {outcome.rollback_pointer}")
        return "\n".join(lines)
    if outcome.attempted:
        lines.append(f"  closed: {', '.join(outcome.closed_roles) or 'none'} relaunched: {outcome.relaunched}")
        lines.append(f"  redispatch: {outcome.redispatch}")
        if not outcome.executed:
            # An attempted run that applied nothing is an idempotent replay, not a recovery.
            lines.append(f"  detail: {outcome.detail}")
    elif outcome.preflight.may_recover:
        lines.append("  (preflight only; re-run with --execute to recover the pair)")
    return "\n".join(lines)


def cmd_sublane_recover_pair(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = RecoverPairRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        implementation_request_journal=getattr(args, "implementation_request_journal", "") or "",
    )
    json_mode = bool(getattr(args, "json", False))
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (  # noqa: E501
        build_live_recover_pair_use_case,
    )

    # The builder binds the owner-APPROVAL journal into the live ops (close/relaunch/actuator
    # requests); the ORIGINAL implementation_request journal flows per-run through the request
    # to the redispatch call, so it is not a builder argument.
    use_case = build_live_recover_pair_use_case(
        repo_root=repo_root, env=dict(os.environ),
        issue=request.issue, lane=request.lane, journal=request.journal,
    )
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if json_mode:
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_recover_pair_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def cmd_sublane_recover_pair_delivery(args: argparse.Namespace) -> int:
    """Drive one owner-approved new-action delivery to an already-active recovered pair."""
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = RecoverPairDeliveryRetryRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        implementation_request_journal=(
            getattr(args, "implementation_request_journal", "") or ""
        ),
        retry_of_action_id=getattr(args, "retry_of_action_id", "") or "",
        prior_zero_send_journal=getattr(args, "prior_zero_send_journal", "") or "",
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (  # noqa: E501
        build_live_recover_pair_delivery_use_case,
    )

    use_case = build_live_recover_pair_delivery_use_case(
        repo_root=repo_root,
        env=dict(os.environ),
        issue=request.issue,
        lane=request.lane,
        journal=request.journal,
    )
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        # Review j#88563 F3: the retry surface reports the SAME typed contract as the main
        # recovery, so an operator reading either sees applied effects and unresolved fates,
        # not just a status token.
        applied = ", ".join(outcome.effects) or "nothing"
        fate = f"\n  unresolved: {', '.join(outcome.unresolved) or 'none'}"
        print(
            f"sublane recover-pair-delivery: {outcome.lane} (issue {outcome.issue})\n"
            f"  action_id: {outcome.action_id or '<invalid>'}\n"
            f"  may_deliver: {outcome.may_deliver} executed: {outcome.executed} "
            f"redispatch: {outcome.redispatch}\n"
            f"  applied: {applied}{fate}\n"
            f"  {outcome.detail}",
            file=sys.stdout,
        )
    return 1 if outcome.is_blocked else 0


def register_sublane_recover_pair_parser(sublane_sub: Any) -> None:
    """Register ``sublane recover-pair`` outside the at-ceiling core CLI module."""
    parser = sublane_sub.add_parser(
        "recover-pair",
        help=(
            "Redmine #13847: recover the exact gateway+worker pair of a hibernated lane "
            "whose fresh launch booted partially (unattested/stale). Default is preflight "
            "only; --execute closes only the bad generation, relaunches, resumes, and "
            "redispatches the original implementation_request exactly-once."
        ),
    )
    parser.add_argument("--issue", required=True, help="Redmine issue the hibernated lane owns")
    parser.add_argument("--lane", required=True, help="Hibernated lane label to recover")
    parser.add_argument(
        "--journal",
        required=True,
        help="Redmine journal of the owner APPROVAL authorizing this destructive recovery "
        "(the resume authorization anchor)",
    )
    parser.add_argument(
        "--implementation-request-journal",
        dest="implementation_request_journal",
        required=True,
        help="Redmine journal of the ORIGINAL implementation_request to re-deliver to the "
        "gateway exactly-once (the fence key + delivery anchor; distinct from --journal)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the guarded close (bad generation only) + relaunch + resume + redispatch",
    )
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output")
    parser.set_defaults(func=cmd_sublane_recover_pair)

    delivery = sublane_sub.add_parser(
        "recover-pair-delivery",
        help=(
            "Redmine #14203 R17: deliver the original implementation_request to an "
            "already-active recovered pair under one explicit new recovery action. The "
            "prior fence row is retained and never released."
        ),
    )
    delivery.add_argument("--issue", required=True)
    delivery.add_argument("--lane", required=True)
    delivery.add_argument(
        "--journal",
        required=True,
        help="New owner-approval journal authorizing this exact recovery-delivery action",
    )
    delivery.add_argument(
        "--implementation-request-journal",
        dest="implementation_request_journal",
        required=True,
        help="The unchanged original implementation_request anchor to deliver",
    )
    delivery.add_argument(
        "--retry-of-action-id",
        dest="retry_of_action_id",
        required=True,
        help="Exact prior pair-recovery action whose fenced delivery was proven zero-send",
    )
    delivery.add_argument(
        "--prior-zero-send-journal",
        dest="prior_zero_send_journal",
        required=True,
        help="Durable outcome journal recording typed/send 0 for the prior action",
    )
    delivery.add_argument(
        "--execute",
        action="store_true",
        help="Perform one guarded, fenced delivery attempt (default: preflight only)",
    )
    add_repo_option(delivery)
    delivery.add_argument("--json", action="store_true")
    delivery.set_defaults(func=cmd_sublane_recover_pair_delivery)

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_recovered_worker_delivery_cli import (  # noqa: E501
        register_sublane_recovered_worker_delivery_parser,
    )

    register_sublane_recovered_worker_delivery_parser(sublane_sub)

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_recovered_pair_pin_reconciliation_cli import (  # noqa: E501
        register_sublane_recovered_pair_pin_reconciliation_parser,
    )

    register_sublane_recovered_pair_pin_reconciliation_parser(sublane_sub)
