"""``sublane recover-gateway`` CLI surface (Redmine #14203).

The owner-facing command wiring for the guarded gateway refresh use case
(:mod:`...application.sublane_gateway_recovery`) — the argument parser, the request builder,
the seam construction, and the text/JSON rendering (the codebase's ``*_cli.py`` split).

The live observation / actuation ops are **deliberately not wired yet** (the #13806 tranche D
precedent: live process mutation lands as a follow-up once the deterministic machinery is
reviewed). Until then every invocation — preflight AND execute — returns a fail-closed typed
:data:`SEAM_UNAVAILABLE_VERDICT` outcome with ZERO process effect and a non-zero exit, so a
broken / premature invocation can never read as a clean preflight or a completed refresh.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from mozyo_bridge.core.state.replacement_transaction_model import norm
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery import (  # noqa: E501
    GatewayRefreshOutcome,
    GatewayRefreshRequest,
    REFRESH_STATUS_REFUSED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    TURN_CLASS_UNOBSERVABLE,
    TURN_REASON_UNKNOWN,
)

#: The verdict the fail-closed staged seam surfaces: the live gateway observation / actuation
#: ops are not wired yet, so NO invocation observes or mutates anything — never a fabricated
#: preflight, never a silent no-op exit 0.
SEAM_UNAVAILABLE_VERDICT = "gateway_refresh_seam_unavailable"


def format_recover_gateway_text(outcome: GatewayRefreshOutcome) -> str:
    lines = [
        f"sublane recover-gateway: {outcome.lane} / {outcome.role} (issue {outcome.issue})",
        f"  turn_class: {outcome.turn_class}  turn_reason: {outcome.turn_reason}",
        f"  verdict: {outcome.verdict}  status: {outcome.status}",
        f"  executed: {outcome.executed}",
    ]
    if outcome.executed:
        lines.append(
            f"  refresh: {outcome.refresh_status or '-'}  "
            f"resume: {outcome.resume_status or '-'}  "
            f"closed_old: {outcome.closed_old_gateway}  "
            f"attested: {outcome.fresh_slot_attested}"
        )
    if outcome.post_close_resume:
        lines.append("  post_close_resume: true")
    if outcome.detail:
        lines.append(f"  detail: {outcome.detail}")
    return "\n".join(lines)


def cmd_sublane_recover_gateway(args: argparse.Namespace) -> int:
    request = GatewayRefreshRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        role=getattr(args, "role", "") or "",
        provider=getattr(args, "provider", "") or "",
        assigned_name=getattr(args, "assigned_name", "") or "",
        locator=getattr(args, "locator", "") or "",
        journal=getattr(args, "journal", "") or "",
        action_id=getattr(args, "action_id", "") or "",
        action_generation=int(getattr(args, "action_generation", 0) or 0),
        gateway_revision=getattr(args, "gateway_revision", "") or "",
        lane_revision=getattr(args, "lane_revision", "") or "",
        lane_generation=getattr(args, "lane_generation", "") or "",
        resume_anchor_journal=getattr(args, "resume_anchor_journal", "") or "",
        resume_gate=getattr(args, "resume_gate", "") or "",
        reason_token=getattr(args, "reason_token", "") or "",
    )
    execute = bool(getattr(args, "execute", False))
    # Fail-closed staged seam (the #13806 tranche D precedent): the live observation /
    # actuation ops are a follow-up, so NOTHING is observed or mutated — the turn class is the
    # honest ``turn_unobservable`` (never a fabricated classification) and the outcome is a
    # typed refusal with a non-zero exit.
    outcome = GatewayRefreshOutcome(
        issue=norm(request.issue), lane=norm(request.lane), role=norm(request.role),
        turn_class=TURN_CLASS_UNOBSERVABLE, turn_reason=TURN_REASON_UNKNOWN,
        verdict=SEAM_UNAVAILABLE_VERDICT, status=REFRESH_STATUS_REFUSED, executed=execute,
        detail=(
            "live gateway observation / actuation ops are not wired yet (follow-up); "
            "zero observation, zero process effect"
        ),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_recover_gateway_text(outcome), file=sys.stdout)
    return 1


def register_sublane_recover_gateway_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "recover-gateway",
        help=(
            "guarded refresh of ONE exact failed lane implementation_gateway (preflight "
            "default; --execute needs a durable owner approval)"
        ),
        description=(
            "Classify a delivered callback's provider turn (the durable journal is the "
            "authority; an unconfirmed delivery / turn start is never a failure) and — with "
            "a positive owner approval — close ONLY the exact approved gateway generation, "
            "relaunch the same durable slot, verify its action-bound attestation, and resume "
            "the EXISTING durable anchor exactly once via the callback recovery rail. The "
            "worker, worktree, branch, and durable route are preserved; the worker / default "
            "coordinator / foreign slots are protected by ordered fail-closed fences."
        ),
    )
    parser.add_argument("--issue", required=True, help="Redmine issue id owning the lane")
    parser.add_argument("--lane", required=True, help="exact lane id")
    parser.add_argument("--role", required=True, help="gateway role token (e.g. codex)")
    parser.add_argument("--provider", required=True, help="gateway provider token")
    parser.add_argument(
        "--assigned-name", required=True, dest="assigned_name",
        help="the gateway's durable herdr assigned name",
    )
    parser.add_argument(
        "--locator", required=True, help="the gateway's live locator pinned at approval time",
    )
    parser.add_argument(
        "--gateway-revision", dest="gateway_revision", default="",
        help="live gateway inventory row revision pinned at approval time",
    )
    parser.add_argument(
        "--lane-revision", dest="lane_revision", default="",
        help="lane lifecycle revision pinned at approval time (--execute: required)",
    )
    parser.add_argument(
        "--lane-generation", dest="lane_generation", default="",
        help="lane lifecycle generation pinned at approval time (--execute: required)",
    )
    parser.add_argument(
        "--journal", default="",
        help="Redmine journal id of the positive owner approval (--execute: required)",
    )
    parser.add_argument(
        "--action-id", dest="action_id", default="",
        help="the exact refresh-gateway:<...> action id the approval names",
    )
    parser.add_argument(
        "--action-generation", dest="action_generation", type=int, default=0,
        help="the immutable approved generation counter (>= 1)",
    )
    parser.add_argument(
        "--resume-anchor-journal", dest="resume_anchor_journal", default="",
        help=(
            "the EXISTING durable anchor journal the fresh gateway must resume "
            "(distinct from the approval journal; never regenerated)"
        ),
    )
    parser.add_argument(
        "--resume-gate", dest="resume_gate", default="",
        help="the durable gate kind the resume anchor carries (e.g. review_request)",
    )
    parser.add_argument(
        "--reason-token", dest="reason_token", default="",
        help=(
            "optional structured turn-failure reason evidence token "
            "(rate_limit / auth / session_stale; anything else collapses to unknown)"
        ),
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="actuate (default is a read-only preflight)",
    )
    parser.add_argument("--json", action="store_true", help="emit the structured outcome")
    parser.set_defaults(func=cmd_sublane_recover_gateway)


__all__ = (
    "SEAM_UNAVAILABLE_VERDICT",
    "cmd_sublane_recover_gateway",
    "format_recover_gateway_text",
    "register_sublane_recover_gateway_parser",
)
