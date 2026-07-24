"""``sublane recover-gateway`` CLI surface (Redmine #14203).

The owner-facing command wiring for the guarded gateway refresh use case
(:mod:`...application.sublane_gateway_recovery`) — the argument parser, the request builder,
the LIVE composition-root construction, and the text/JSON rendering (the codebase's
``*_cli.py`` split; review j#87356 F1 — the surface must connect the live use case, never a
staged seam).

The live composition (the #13806 recover-stale precedent, reused): the exact-generation
close / relaunch / attestation port is the #13806 :class:`LiveRecoveryActuatorPort` over the
field-adapted pin; the observations + resume rail are :class:`LiveGatewayRecoveryOps`; the
FRESH durable journal boundary is :class:`LiveRedmineJournalSource` — when the trusted
credentials are unconfigured the turn classification honestly reports ``turn_unobservable``
(fail-closed: a refresh is then never actionable; nothing is fabricated). A construction
error — a repo / workspace identity that cannot be resolved — is a fail-closed typed outcome
with a non-zero exit, never a fabricated preflight.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mozyo_bridge.core.state.replacement_transaction import (
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import norm
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery import (  # noqa: E501
    GatewayRefreshOutcome,
    GatewayRefreshRequest,
    GatewayRefreshUseCase,
    REFRESH_STATUS_REFUSED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    TURN_CLASS_UNOBSERVABLE,
    TURN_REASON_UNKNOWN,
    gateway_refresh_action_id,
)

#: The verdict a fail-closed construction error surfaces (a missing repo / workspace
#: identity), so a broken invocation never silently reads as a clean preflight.
SEAM_UNAVAILABLE_VERDICT = "gateway_refresh_seam_error"


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


def _run_live_refresh(
    args: argparse.Namespace, request: GatewayRefreshRequest, *, execute: bool
) -> GatewayRefreshOutcome:
    """Construct the LIVE use case (real inventory + actuation + resume rail) and run it.

    The live adapters are imported lazily (they import the use case module for the request /
    ops types). A construction error — an unresolvable repo / workspace identity — is a
    fail-closed typed outcome, never a fabricated preflight (the recover-stale precedent).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery_live import (  # noqa: E501
        LiveGatewayRecoveryOps,
        port_pin_request,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live import (  # noqa: E501
        LiveRecoveryActuatorPort,
    )

    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    try:
        workspace_id = repo_scope_workspace_id(repo_root)
    except Exception:  # noqa: BLE001 - an unresolvable workspace identity fails closed
        workspace_id = ""
    if not norm(workspace_id):
        return GatewayRefreshOutcome(
            issue=norm(request.issue), lane=norm(request.lane), role=norm(request.role),
            turn_class=TURN_CLASS_UNOBSERVABLE, turn_reason=TURN_REASON_UNKNOWN,
            verdict=SEAM_UNAVAILABLE_VERDICT, status=REFRESH_STATUS_REFUSED,
            executed=execute,
            detail="could not resolve the repo workspace identity; zero process effect",
        )
    # The transaction key the use case will derive (best-effort; the use case re-derives and
    # refuses on incomplete inputs before the port is ever exercised).
    try:
        action_id = gateway_refresh_action_id(
            lane_id=request.lane, role=request.role, provider=request.provider,
            assigned_name=request.assigned_name, locator=request.locator,
        )
        key = ReplacementTransactionKey(workspace_id, action_id)
    except Exception:  # noqa: BLE001 - incomplete identity => the use case refuses downstream
        key = ReplacementTransactionKey(workspace_id, "refresh-gateway:pending")
    store = ReplacementTransactionStore()
    actuation_port = LiveRecoveryActuatorPort(
        repo_root=repo_root, request=port_pin_request(request), store=store, key=key,
    )
    # The FRESH durable journal boundary (#13889): the credential-gated live Redmine source.
    # Unconfigured credentials leave the reader unwired — the turn classification then
    # honestly reports ``turn_unobservable`` (fail-closed), never a fabricated absence.
    journal_reader = None
    journal_reader_fresh = False
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        source = LiveRedmineJournalSource.from_environment()
        journal_reader = source.read_entries
        journal_reader_fresh = True
    except Exception:  # noqa: BLE001 - no live durable boundary => turn_unobservable
        journal_reader = None
        journal_reader_fresh = False
    ops = LiveGatewayRecoveryOps(
        repo_root=repo_root, request=request,
        journal_reader=journal_reader, journal_reader_fresh=journal_reader_fresh,
    )
    use_case = GatewayRefreshUseCase(
        store, actuation_port, ops, workspace_id=workspace_id,
    )
    return use_case.run(request, execute=execute)


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
    outcome = _run_live_refresh(args, request, execute=execute)
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_recover_gateway_text(outcome), file=sys.stdout)
    # A construction-error refusal is a non-zero exit so a caller never mistakes it for a
    # completed refresh; a preflight that merely reports a blocker is exit 0.
    return 1 if outcome.is_blocked or outcome.verdict == SEAM_UNAVAILABLE_VERDICT else 0


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
            "the EXISTING durable anchor exactly once via the governed handoff rail. The "
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
