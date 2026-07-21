"""``sublane recover-stale`` CLI surface (Redmine #13806 tranche D).

The owner-facing command wiring for the stale standard-sublane worker recovery use case
(:mod:`...application.sublane_stale_worker_recovery`). Kept as a sibling CLI leaf — the
argument parser, the request builder, the live-seam construction, and the text/JSON rendering
— so the use case module holds only the pure request / outcome model + the guarded decision
flow (the codebase's ``*_cli.py`` split). The live inventory / actuation adapters are imported
lazily to avoid an import cycle (they import the use case module for the request / ops types).
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E501
    RECOVERY_REFUSED,
    RecoveryOutcome,
    RecoveryRequest,
    StaleWorkerRecoveryUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
    stale_worker_recovery_action_id,
)

#: The verdict a fail-closed construction error surfaces (a missing repo / workspace identity),
#: so a broken invocation never silently reads as a clean preflight.
SEAM_UNAVAILABLE_VERDICT = "recovery_seam_error"


def format_recover_text(outcome: RecoveryOutcome) -> str:
    lines = [
        f"sublane recover-stale: {outcome.lane} / {outcome.role} (issue {outcome.issue})",
        f"  verdict: {outcome.verdict}  status: {outcome.status}",
        f"  executed: {outcome.executed}",
    ]
    if outcome.executed:
        lines.append(
            f"  recovery: {outcome.recovery_status or '-'}  "
            f"redispatch: {outcome.redispatch_status or '-'}  "
            f"closed_old: {outcome.closed_old_worker}"
        )
    if outcome.post_close_resume:
        lines.append(
            "  post_close_resume: true"
            + (
                f"  resume_authorization: {outcome.resume_authorization}"
                if outcome.resume_authorization
                else ""
            )
        )
    if outcome.detail:
        lines.append(f"  detail: {outcome.detail}")
    return "\n".join(lines)


def _run_live_recovery(
    args: argparse.Namespace, request: RecoveryRequest, *, execute: bool
) -> RecoveryOutcome:
    """Construct the LIVE use case (real inventory + actuation + redispatch) and run it.

    The live adapters are imported lazily to avoid an import cycle (they import the use case
    module for the request / ops types). A construction error — a repo / workspace identity that
    cannot be resolved — is a fail-closed typed outcome, never a fabricated preflight.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live import (  # noqa: E501
        LiveRecoveryActuatorPort,
        LiveStaleWorkerRecoveryOps,
    )

    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    try:
        workspace_id = repo_scope_workspace_id(repo_root)
    except Exception:  # noqa: BLE001 - an unresolvable workspace identity fails closed
        workspace_id = ""
    if not norm(workspace_id):
        return RecoveryOutcome(
            issue=norm(request.issue), lane=norm(request.lane), role=norm(request.role),
            verdict=SEAM_UNAVAILABLE_VERDICT, status=RECOVERY_REFUSED, executed=execute,
            detail="could not resolve the repo workspace identity; zero process effect",
        )
    # The transaction key the use case will derive (best-effort; the use case re-derives and
    # refuses on incomplete inputs before the port is ever exercised).
    try:
        action_id = stale_worker_recovery_action_id(
            lane_id=request.lane, role=request.role, provider=request.provider,
            assigned_name=request.assigned_name, locator=request.locator,
        )
        key = ReplacementTransactionKey(workspace_id, action_id)
    except Exception:  # noqa: BLE001 - incomplete identity => the use case refuses downstream
        key = ReplacementTransactionKey(workspace_id, "recover:pending")
    store = ReplacementTransactionStore()
    actuation_port = LiveRecoveryActuatorPort(
        repo_root=repo_root, request=request, store=store, key=key,
    )
    ops = LiveStaleWorkerRecoveryOps(repo_root=repo_root, request=request)
    use_case = StaleWorkerRecoveryUseCase(
        store, actuation_port, ops, workspace_id=workspace_id,
    )
    return use_case.run(request, execute=execute)


def cmd_sublane_recover_stale(args: argparse.Namespace) -> int:
    request = RecoveryRequest(
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
        expected_gate=getattr(args, "expected_gate", "") or "",
        next_semantic_action=getattr(args, "next_semantic_action", "") or "",
        supersede=bool(getattr(args, "supersede", False)),
        resume_journal=getattr(args, "resume_journal", "") or "",
    )
    execute = bool(getattr(args, "execute", False))
    outcome = _run_live_recovery(args, request, execute=execute)
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_recover_text(outcome), file=sys.stdout)
    # A staged-seam refusal is a non-zero exit so a caller never mistakes it for a completed
    # recovery; a preflight (once wired) that merely reports a blocker is exit 0.
    return 1 if outcome.is_blocked or outcome.verdict == SEAM_UNAVAILABLE_VERDICT else 0


def register_sublane_recover_stale_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "recover-stale",
        help=(
            "Redmine #13806: recover the exact stale standard-sublane worker of a lane whose "
            "worker process vanished after a turn. Default is read-only preflight; --execute "
            "requires a positive generation-bound owner approval and closes only that worker "
            "(never the gateway / coordinator / a foreign slot), byte-preserving the worktree."
        ),
    )
    for flag, dest, help_text in (
        ("--issue", "issue", "Redmine issue id owning the lane"),
        ("--lane", "lane", "Exact lane id/label of the stale worker"),
        ("--role", "role", "Exact provider role of the worker"),
        ("--provider", "provider", "Exact provider of the worker"),
        ("--assigned-name", "assigned_name", "Exact managed assigned name"),
        ("--locator", "locator", "Exact stale (old) process locator"),
    ):
        parser.add_argument(flag, dest=dest, required=True, help=help_text)
    for flag, dest, help_text in (
        ("--journal", "journal", "Positive owner approval journal id (--execute)"),
        ("--action-id", "action_id", "Exact recover:<lane>:<role>:<provider>:<name>:<locator> id"),
        (
            "--worker-revision",
            "worker_revision",
            "Live worker inventory row revision pinned at approval (preflight generation gate; "
            "distinct from the lane lifecycle revision)",
        ),
        (
            "--lane-revision",
            "lane_revision",
            "Lane LIFECYCLE revision pinned at approval (close-boundary preservation fence)",
        ),
        (
            "--lane-generation",
            "lane_generation",
            "Lane LIFECYCLE generation pinned at approval (close-boundary preservation fence)",
        ),
        ("--expected-gate", "expected_gate", "The durable gate the fresh worker must resume"),
        (
            "--next-semantic-action",
            "next_semantic_action",
            "The single semantic action to redispatch exactly once",
        ),
        (
            "--resume-journal",
            "resume_journal",
            "Owner RE-approval journal for a post-close resume — a SEPARATE authority from "
            "--journal (which stays the transaction's immutable stored decision/continuation "
            "anchor). Lets a fresh re-approval coexist with the same-action CAS; empty resumes "
            "on the original anchor",
        ),
    ):
        parser.add_argument(flag, dest=dest, default="", help=help_text)
    parser.add_argument(
        "--action-generation", dest="action_generation", type=int, default=0,
        help="Immutable approved generation counter (>= 1) (--execute)",
    )
    parser.add_argument(
        "--supersede", action="store_true",
        help=(
            "With a higher --action-generation, re-anchor a zero-effect stuck same-action "
            "transaction to the corrected evidence (converges a mis-bound residue without raw "
            "DB; refused once any close / launch / send happened)"
        ),
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Apply owner-approved recovery; otherwise read-only preflight only",
    )
    from mozyo_bridge.application.cli_common import add_repo_option

    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_recover_stale)


__all__ = (
    "SEAM_UNAVAILABLE_VERDICT",
    "cmd_sublane_recover_stale",
    "format_recover_text",
    "register_sublane_recover_stale_parser",
)
