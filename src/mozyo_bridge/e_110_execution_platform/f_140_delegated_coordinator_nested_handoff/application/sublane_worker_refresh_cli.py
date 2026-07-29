"""``sublane refresh-worker`` CLI surface (Redmine #14661).

The owner-facing command wiring for the guarded live-worker refresh use case
(:mod:`...application.sublane_worker_refresh`) — the argument parser, the request builder, the
LIVE composition-root construction, and the text/JSON rendering (the codebase's ``*_cli.py``
split; the #14203 j#87356 F1 rule — the surface must connect the live use case, never a staged
seam).

The live composition mirrors its two sibling recovery surfaces exactly: the exact-generation
close / relaunch / attestation port is the #13806 :class:`LiveRecoveryActuatorPort` over the
field-adapted pin; the observations + resume rail are :class:`LiveWorkerRefreshOps`; the FRESH
durable journal boundary is :class:`LiveRedmineJournalSource` — when the trusted credentials
are unconfigured the turn classification honestly reports ``turn_unobservable`` (fail-closed: a
refresh is then never actionable; nothing is fabricated). A construction error — a repo /
workspace identity that cannot be resolved — is a fail-closed typed outcome with a non-zero
exit, never a fabricated preflight.
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh import (  # noqa: E501
    WORKER_REFRESH_STATUS_REFUSED,
    WorkerRefreshOutcome,
    WorkerRefreshRequest,
    WorkerRefreshUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    TURN_CLASS_UNOBSERVABLE,
    TURN_REASON_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
    worker_refresh_action_id,
)

#: The verdict a fail-closed construction error surfaces (a missing repo / workspace identity),
#: so a broken invocation never silently reads as a clean preflight.
SEAM_UNAVAILABLE_VERDICT = "worker_refresh_seam_error"


def format_refresh_worker_text(outcome: WorkerRefreshOutcome) -> str:
    lines = [
        f"sublane refresh-worker: {outcome.lane} / {outcome.role} (issue {outcome.issue})",
        f"  turn_class: {outcome.turn_class}  turn_reason: {outcome.turn_reason}",
        f"  verdict: {outcome.verdict}  status: {outcome.status}",
        f"  executed: {outcome.executed}",
    ]
    if outcome.executed:
        lines.append(
            f"  refresh: {outcome.refresh_status or '-'}  "
            f"resume: {outcome.resume_status or '-'}  "
            f"closed_old: {outcome.closed_old_worker}  "
            f"attested: {outcome.fresh_slot_attested}"
        )
    if outcome.post_close_resume:
        lines.append("  post_close_resume: true")
    # The closed axis token is always shown; its runbook only when the axis is actually
    # blocking (an ``ok`` axis has nothing to recover) — the #14475 j#88477 F2 rendering.
    lines.append(f"  launch_authority: {outcome.launch_authority_reason}")
    if outcome.launch_authority_runbook:
        lines.append(f"  launch_authority_recovery: {outcome.launch_authority_runbook}")
    # Shown only when a launch fence actually fired (#14480): unlike the authority axis, this
    # field's empty value means "the launch leg never fenced", and printing a placeholder would
    # invite reading absence-of-failure as a diagnosis.
    if outcome.launch_failure_reason:
        lines.append(f"  launch_failure: {outcome.launch_failure_reason}")
    # The exact marker a positive owner approval must carry (j#92487 F1). Shown on the
    # read-only preflight — the moment the operator is deciding whether to approve — so the
    # approval contract is producible rather than merely enforceable.
    if outcome.required_approval_marker and not outcome.executed:
        lines.append(f"  required_approval_marker: {outcome.required_approval_marker}")
    if outcome.detail:
        lines.append(f"  detail: {outcome.detail}")
    return "\n".join(lines)


def _run_live_refresh(
    args: argparse.Namespace, request: WorkerRefreshRequest, *, execute: bool
) -> WorkerRefreshOutcome:
    """Construct the LIVE use case (real inventory + actuation + resume rail) and run it.

    The live adapters are imported lazily (they import the use case module for the request /
    ops types). A construction error — an unresolvable repo / workspace identity — is a
    fail-closed typed outcome, never a fabricated preflight.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh_live import (  # noqa: E501
        LiveWorkerRefreshOps,
        SettledCloseBoundaryPort,
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
        return WorkerRefreshOutcome(
            issue=norm(request.issue), lane=norm(request.lane), role=norm(request.role),
            turn_class=TURN_CLASS_UNOBSERVABLE, turn_reason=TURN_REASON_UNKNOWN,
            verdict=SEAM_UNAVAILABLE_VERDICT, status=WORKER_REFRESH_STATUS_REFUSED,
            executed=execute,
            detail="could not resolve the repo workspace identity; zero process effect",
        )
    # The transaction key the use case will derive (best-effort; the use case re-derives and
    # refuses on incomplete inputs before the port is ever exercised).
    try:
        action_id = worker_refresh_action_id(
            lane_id=request.lane, role=request.role, provider=request.provider,
            assigned_name=request.assigned_name, locator=request.locator,
            revision=request.worker_revision,
        )
        key = ReplacementTransactionKey(workspace_id, action_id)
    except Exception:  # noqa: BLE001 - incomplete identity => the use case refuses downstream
        key = ReplacementTransactionKey(workspace_id, "refresh-worker:pending")
    store = ReplacementTransactionStore()
    shared_port = LiveRecoveryActuatorPort(
        repo_root=repo_root, request=port_pin_request(request), store=store, key=key,
    )
    # The FRESH durable journal boundary (#13889): the credential-gated live Redmine source.
    # Unconfigured credentials leave the reader unwired — the turn classification then honestly
    # reports ``turn_unobservable`` (fail-closed), never a fabricated absence of progress.
    journal_reader = None
    journal_reader_fresh = False
    issuer_resolver = None
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        source = LiveRedmineJournalSource.from_environment()
        journal_reader = source.read_entries
        journal_reader_fresh = True
        # The approval issuer authority (#14661 j#92494): the anchor issue's own author, read
        # fresh from the same trusted source. Identifier only — never the display name.
        issuer_resolver = source.read_issue_author_id
    except Exception:  # noqa: BLE001 - no live durable boundary => turn_unobservable
        journal_reader = None
        journal_reader_fresh = False
        issuer_resolver = None
    ops = LiveWorkerRefreshOps(
        repo_root=repo_root, request=request,
        journal_reader=journal_reader, journal_reader_fresh=journal_reader_fresh,
        issuer_resolver=issuer_resolver,
    )
    # Review j#92487 F2: the shared close boundary admits a close on every non-``working``
    # runtime state (including ``blocked`` and an unreadable ``unknown``). Wrap it so the
    # destructive edge re-requires the positively-settled worker the preflight demanded.
    actuation_port = SettledCloseBoundaryPort(
        inner=shared_port, ops=ops, request=request,
    )
    use_case = WorkerRefreshUseCase(store, actuation_port, ops, workspace_id=workspace_id)
    return use_case.run(request, execute=execute)


def cmd_sublane_refresh_worker(args: argparse.Namespace) -> int:
    request = WorkerRefreshRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        role=getattr(args, "role", "") or "",
        provider=getattr(args, "provider", "") or "",
        assigned_name=getattr(args, "assigned_name", "") or "",
        locator=getattr(args, "locator", "") or "",
        journal=getattr(args, "journal", "") or "",
        action_id=getattr(args, "action_id", "") or "",
        action_generation=int(getattr(args, "action_generation", 0) or 0),
        worker_revision=getattr(args, "worker_revision", "") or "",
        lane_revision=getattr(args, "lane_revision", "") or "",
        lane_generation=getattr(args, "lane_generation", "") or "",
        anchor_issue=getattr(args, "anchor_issue", "") or "",
        resume_anchor_journal=getattr(args, "resume_anchor_journal", "") or "",
        resume_gate=getattr(args, "resume_gate", "") or "",
        reason_token=getattr(args, "reason_token", "") or "",
    )
    execute = bool(getattr(args, "execute", False))
    outcome = _run_live_refresh(args, request, execute=execute)
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_refresh_worker_text(outcome), file=sys.stdout)
    # A construction-error refusal is a non-zero exit so a caller never mistakes it for a
    # completed refresh; a preflight that merely reports a blocker is exit 0.
    return 1 if outcome.is_blocked or outcome.verdict == SEAM_UNAVAILABLE_VERDICT else 0


def register_sublane_refresh_worker_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "refresh-worker",
        help=(
            "guarded refresh of ONE exact LIVE turn-ended sublane worker that produced no "
            "durable progress (preflight default; --execute needs a durable owner approval)"
        ),
        description=(
            "Classify a delivered anchor's WORKER provider turn — bound to the exact Redmine "
            "anchor, lane generation, and participant revision; the durable journal is the "
            "authority, and an unconfirmed delivery / turn start or an unsettled runtime is "
            "never a failure — and, with a positive owner approval, close ONLY the exact "
            "approved worker generation, relaunch the same durable slot, verify its "
            "action-bound attestation, and resume the EXISTING durable anchor exactly once "
            "via the governed handoff rail. The dirty worktree, branch, and durable route are "
            "preserved byte-for-byte; the lane gateway, default coordinator, and foreign "
            "slots are protected by ordered fail-closed fences. This is a SEPARATE admission "
            "from 'sublane recover-stale', which recovers a VANISHED worker and is not "
            "loosened by this surface."
        ),
    )
    parser.add_argument("--issue", required=True, help="Redmine issue id owning the lane")
    parser.add_argument("--lane", required=True, help="exact lane id")
    parser.add_argument("--role", required=True, help="worker role token (e.g. claude)")
    parser.add_argument("--provider", required=True, help="worker provider token")
    parser.add_argument(
        "--assigned-name", required=True, dest="assigned_name",
        help="the worker's durable herdr assigned name",
    )
    parser.add_argument(
        "--locator", required=True, help="the worker's live locator pinned at approval time",
    )
    parser.add_argument(
        "--worker-revision", dest="worker_revision", default="",
        help=(
            "live worker inventory row revision pinned at approval time (REQUIRED for a "
            "destructive refresh; an empty pin never matches)"
        ),
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
        help=(
            "Redmine journal id of the positive owner approval (--execute: required). The "
            "journal must exist uniquely at a fresh durable read AND carry exactly one "
            "canonical structured approval marker whose every field matches this action; run "
            "the preflight and copy its required_approval_marker. A prose mention, a quoted "
            "command or a neighbouring round is refused with zero close"
        ),
    )
    parser.add_argument(
        "--action-id", dest="action_id", default="",
        help="the exact refresh-worker:<...> action id the approval names",
    )
    parser.add_argument(
        "--action-generation", dest="action_generation", type=int, default=0,
        help="the immutable approved generation counter (>= 1)",
    )
    parser.add_argument(
        "--anchor-issue", dest="anchor_issue", default="",
        help=(
            "the issue carrying the anchor/approval journals when it differs from the lane's "
            "owning --issue (parent-lane/child-issue topology); default = --issue"
        ),
    )
    parser.add_argument(
        "--resume-anchor-journal", dest="resume_anchor_journal", default="",
        help=(
            "the EXISTING durable anchor journal the fresh worker must resume (distinct from "
            "the approval journal; never regenerated)"
        ),
    )
    parser.add_argument(
        "--resume-gate", dest="resume_gate", default="",
        help="the durable gate kind the resume anchor carries (e.g. review_result)",
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
    parser.set_defaults(func=cmd_sublane_refresh_worker)


__all__ = (
    "SEAM_UNAVAILABLE_VERDICT",
    "cmd_sublane_refresh_worker",
    "format_refresh_worker_text",
    "register_sublane_refresh_worker_parser",
)
