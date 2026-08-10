"""``sublane audit-failure-terminal record`` — the coordinator's decision command (#15166).

The writer half of the route :mod:`.retire_superseded_audit_failure` reads. Scope decision j#102081
ruled out enumerating permitted lanes in the package, and design direction j#102092 ruled that the
binding must instead be an actual coordinator judgement recorded verifiably. This command records
that judgement: it resolves the lane's identity from durable state, re-measures the world it is
deciding about, and writes one :class:`...audit_failure_terminal_decision.TerminalDecision`.

**CURRENT STATE: a decision recorded here authorizes NO retire** (review j#102582 finding 2). The
route that reads it is inert — see
:data:`...superseded_audit_failure_terminal.RECEIPT_AUTHORITY_RESOLVABLE` — because nothing here
can establish that the writer really was the coordinator runtime, and the coordinator's ruling on
consultation j#102184 holds it that way until #15195 supplies a Herdr-runtime-issued receipt.
Until then a record written by this command is a PRE-AUTHORITY DIAGNOSTIC record: it is useful for
staging and inspecting a terminal disposition, and it unlocks nothing. Everything below describes
the contract that becomes an authority once #15195 lands, not one operating today.

**Why it re-measures instead of taking the operator's word.** A decision that recorded whatever argv
said would be a caller-supplied authority wearing a store's clothes (#14539 j#91797 F2). So every
identity it stores is resolved here, at decision time, from an independent source:

- ``workspace_id`` / ``lane_generation`` / ``lane_revision`` from the lane lifecycle row;
- ``head`` from the lane checkout's own ``rev-parse HEAD``;
- ``integration_branch`` from the repository's COMMITTED config.

Only the four Redmine identities — the source issue, the audit journal, and the successor issue and
its approved review journal — come from the operator, because THAT is the judgement: which audit
failure this is, and which successor supersedes it. Everything else is measurement, and the retire
re-measures all of it again at action time and refuses on any drift.

The command writes ONLY the decision record. It touches no lane lifecycle row, no process, no
worktree, no branch and no Redmine journal; recording a decision never retires anything.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

DECISION_OK = "recorded"
DECISION_TARGET_UNRESOLVED = "lane_target_unresolved"
DECISION_HEAD_UNMEASURED = "lane_head_unmeasured"
DECISION_BRANCH_UNRESOLVED = "committed_integration_branch_unresolved"
DECISION_REFUSED = "decision_refused"


def _lane_head(worktree: str, branch: str) -> str:
    """The lane checkout's ACTUAL head, or ``""`` (read-only probe).

    Identity first, exactly as :func:`...retire_admissibility.measure_lane_change` establishes it:
    the checkout must be attached to the branch it was named for, or it has nothing to say about
    that branch. A detached HEAD prints no ``refs/heads/`` symbolic name and yields ``""``.
    """
    checkout = str(worktree or "").strip()
    wanted = str(branch or "").strip()
    if not checkout or not wanted or wanted == "HEAD":
        return ""

    def _git(*argv: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "-C", checkout, *argv], text=True, capture_output=True
            )
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None

    symbolic = _git("rev-parse", "--symbolic-full-name", "HEAD")
    if symbolic is None or str(symbolic).strip() != f"refs/heads/{wanted}":
        return ""
    return str(_git("rev-parse", "HEAD") or "").strip().lower()


def cmd_audit_failure_terminal_record(args: argparse.Namespace) -> int:
    """Record ONE coordinator audit-failure terminal decision for a lane."""
    from mozyo_bridge.core.state.audit_failure_terminal_decision import (
        AuditFailureTerminalDecisionError,
        AuditFailureTerminalDecisionStore,
        TerminalDecision,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
        resolve_retire_evidence_target,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_superseded_failure import (  # noqa: E501
        committed_integration_branch,
    )
    from mozyo_bridge.shared.paths import resolve_repo_root

    emit: Callable[[dict], None] = lambda payload: print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if getattr(args, "json", False)
        else "\n".join(f"{key}: {value}" for key, value in sorted(payload.items()))
    )

    repo_root = Path(resolve_repo_root(getattr(args, "repo", None)))
    home = getattr(args, "home", None)

    # The lane's own identity, from durable state. Never argv: an identity the caller supplies
    # fences nothing, and this record's whole purpose is to be an independent expectation later.
    target = resolve_retire_evidence_target(args, repo_root, home=home)
    if target is None:
        emit(
            {
                "status": DECISION_TARGET_UNRESOLVED,
                "detail": (
                    "the lane lifecycle row for --lane-label did not resolve to a workspace, a "
                    "positive generation and a positive revision; nothing was recorded"
                ),
            }
        )
        return 2

    branch = committed_integration_branch(repo_root)
    if not branch:
        emit(
            {
                "status": DECISION_BRANCH_UNRESOLVED,
                "detail": (
                    "the repository's committed sublane_integration.integration_branch is unset or "
                    "unreadable, so the decision has no integration branch to be about"
                ),
            }
        )
        return 2

    head = _lane_head(getattr(args, "worktree", "") or "", target.lane)
    if not head:
        emit(
            {
                "status": DECISION_HEAD_UNMEASURED,
                "detail": (
                    "--worktree did not resolve to a checkout attached to the lane branch, so the "
                    "head this decision would be about could not be measured"
                ),
            }
        )
        return 2

    try:
        recorded = AuditFailureTerminalDecisionStore(home=home).record(
            repo_root=repo_root,
            decision=TerminalDecision(
                workspace_id=target.workspace,
                lane_id=target.lane,
                decision_id="",  # minted by the store; a caller never supplies one
                lane_generation=target.lane_generation,
                lane_revision=target.revision,
                issue=str(getattr(args, "issue", "") or ""),
                audit_journal=str(getattr(args, "audit_journal", "") or ""),
                successor_issue=str(getattr(args, "successor_issue", "") or ""),
                successor_review_journal=str(
                    getattr(args, "successor_review_journal", "") or ""
                ),
                head=head,
                integration_branch=branch,
            ),
        )
    except AuditFailureTerminalDecisionError as exc:
        emit({"status": DECISION_REFUSED, "detail": str(exc)})
        return 2

    payload = dict(recorded.as_payload())
    payload["status"] = DECISION_OK
    emit(payload)
    return 0


def register_audit_failure_terminal_decision(
    sublane_sub, *, add_repo_option: Callable[[argparse.ArgumentParser], None]
) -> None:
    """Register ``sublane audit-failure-terminal`` on the ``sublane`` subparser group."""
    parser = sublane_sub.add_parser(
        "audit-failure-terminal",
        help=(
            "Redmine #15166: stage the coordinator's decision that ONE lane's independent-audit "
            "failure may terminally retire, superseded by a named successor's approved review. "
            "**A record written here currently authorizes NO retire**: the route that reads it is "
            "held as a typed refusal until #15195 supplies a Herdr-runtime-issued receipt "
            "(#15195 blocks #15166), so this is a PRE-AUTHORITY DIAGNOSTIC record. Recording one "
            "retires nothing and unlocks nothing."
        ),
    )
    actions = parser.add_subparsers(dest="audit_failure_terminal_command", required=True)
    record = actions.add_parser(
        "record",
        help=(
            "Record the decision for --lane-label. The lane's workspace, generation, revision and "
            "head, and the committed integration branch, are MEASURED here — only the four Redmine "
            "identities are the operator's judgement. The retire re-measures every one of them at "
            "action time and refuses on any drift. **It then refuses anyway** with "
            "`coordinator_receipt_authority_unresolvable` until #15195 lands: the once-only "
            "binding to the lifecycle revision is part of the contract that becomes an authority "
            "then, not a permission this record grants now."
        ),
    )
    record.add_argument("--issue", required=True, help="The source Redmine issue id")
    record.add_argument(
        "--lane-label",
        dest="lane_label",
        required=True,
        help="The lane to decide about (its lifecycle row supplies workspace / generation / revision)",
    )
    record.add_argument(
        "--worktree",
        required=True,
        help="The lane checkout, read-only, to measure the head this decision is about",
    )
    record.add_argument(
        "--audit-journal",
        dest="audit_journal",
        required=True,
        help="The independent-audit journal id on --issue that recorded the failed round",
    )
    record.add_argument(
        "--successor-issue",
        dest="successor_issue",
        required=True,
        help="The issue whose approved review reached the acceptance this lane did not",
    )
    record.add_argument(
        "--successor-review-journal",
        dest="successor_review_journal",
        required=True,
        help="That successor's approved `## Gate: review` journal id",
    )
    add_repo_option(record)
    record.set_defaults(func=cmd_audit_failure_terminal_record)


__all__ = (
    "DECISION_BRANCH_UNRESOLVED",
    "DECISION_HEAD_UNMEASURED",
    "DECISION_OK",
    "DECISION_REFUSED",
    "DECISION_TARGET_UNRESOLVED",
    "cmd_audit_failure_terminal_record",
    "register_audit_failure_terminal_decision",
)
