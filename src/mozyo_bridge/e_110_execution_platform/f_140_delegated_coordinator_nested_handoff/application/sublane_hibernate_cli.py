"""`mozyo-bridge sublane hibernate` text rendering + thin CLI handler + parser.

The presentation leaf of the hibernate use case (Redmine #13682 / #13811), split out of
:mod:`sublane_hibernate` so the use-case module stays under the module-health ceiling
(mirroring how the parser was already carried outside the at-ceiling core CLI module). This
module holds ONLY presentation: it renders a :class:`HibernateOutcome` to text, builds the
:class:`HibernateRequest` from parsed args, and registers the ``sublane hibernate``
subparser. All policy / fail-closed logic lives in :mod:`sublane_hibernate`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from mozyo_bridge.core.state.lane_lifecycle import RELEASE_PARTIAL, LaneLifecycleStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
    HibernateAssertions,
    HibernateOutcome,
    HibernateRequest,
    LiveSublaneHibernateOps,
    SublaneHibernateUseCase,
)


def format_hibernate_text(outcome: HibernateOutcome) -> str:
    # A project-gateway lane owns a scope, not an issue: name the scope it is bound to (the
    # ``issue`` shown is then only the decision anchor's issue).
    owner = (
        f"project_scope {outcome.project_scope}"
        if outcome.project_scope
        else f"issue {outcome.issue}"
    )
    lines = [
        f"sublane hibernate: {outcome.lane} ({owner})",
        f"  may_hibernate: {outcome.preflight.may_hibernate} executed: {outcome.executed}",
    ]
    if outcome.already_hibernated:
        lines.append("  lane already hibernated (idempotent resume)")
    if outcome.is_blocked:
        # Redmine #13843: render the release-boundary reasons alongside the preflight ones.
        lines.append(
            "  -> fail-closed blocked: " + ", ".join(outcome.blocked_reasons)
        )
        if outcome.transition is not None and not outcome.transition.applied:
            lines.append(f"  commit refused: {outcome.transition.reason}")
        return "\n".join(lines)
    if outcome.transition is not None:
        lines.append(
            f"  commit: applied={outcome.transition.applied} "
            f"reason={outcome.transition.reason}"
        )
    if outcome.release is not None:
        rel = outcome.release
        lines.append(f"  release: {rel.process_release} ({rel.detail})")
        for role, locator in rel.closed:
            lines.append(f"    - closed {role} {locator}")
        for role, locator, detail in rel.failed:
            lines.append(f"    ! close failed {role} {locator}: {detail}")
    # Redmine #13843: a released lane whose post-release check found unexpected residue is a
    # WITHHELD success, not a clean one — surface the recovery next-action and exit non-zero.
    if outcome.success_withheld:
        lines.append("  -> success WITHHELD: post-release worktree residue detected")
        if outcome.recovery_detail:
            lines.append(f"     recovery: {outcome.recovery_detail}")
    if not outcome.executed and outcome.preflight.may_hibernate:
        lines.append("  (preflight only; re-run with --execute to hibernate the lane)")
    return "\n".join(lines)


def cmd_sublane_hibernate(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = HibernateRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        project_scope=getattr(args, "project_scope", "") or "",
        expected_lane_generation=getattr(args, "expected_lane_generation", "") or "",
        expected_revision=getattr(args, "expected_revision", "") or "",
        assertions=HibernateAssertions(
            explicitly_parked=bool(getattr(args, "explicitly_parked", False)),
            callbacks_drained=bool(getattr(args, "callbacks_drained", False)),
            no_review_pending=bool(getattr(args, "no_review_pending", False)),
            no_owner_approval_pending=bool(
                getattr(args, "no_owner_approval_pending", False)
            ),
            no_integration_pending=bool(getattr(args, "no_integration_pending", False)),
            no_pending_prompt=bool(getattr(args, "no_pending_prompt", False)),
            not_working=bool(getattr(args, "not_working", False)),
            worktree_clean=bool(getattr(args, "worktree_clean", False)),
            boundary_recorded=bool(getattr(args, "boundary_recorded", False)),
            review_approved=bool(getattr(args, "review_approved", False)),
            staging_integrated=bool(getattr(args, "staging_integrated", False)),
            required_ci_green=bool(getattr(args, "required_ci_green", False)),
            dogfood_delegated=bool(getattr(args, "dogfood_delegated", False)),
            commits_pushed=bool(getattr(args, "commits_pushed", False)),
        ),
    )
    json_mode = bool(getattr(args, "json", False))
    ops = LiveSublaneHibernateOps(repo_root=repo_root, env=dict(os.environ))
    use_case = SublaneHibernateUseCase(ops=ops, store=LaneLifecycleStore())
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if json_mode:
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_hibernate_text(outcome), file=sys.stdout)
    # Redmine #13843: a withheld success (post-release residue) is not a clean success — it
    # must exit non-zero so the coordinator converges to the recovery / boundary-record path.
    if outcome.is_blocked or outcome.success_withheld:
        return 1
    # Review F5: a partial (incomplete) release under --execute still needs a re-drive, so it
    # is not a clean exit — surface it non-zero rather than reporting a fully-actuated success.
    if (
        outcome.executed
        and outcome.release is not None
        and outcome.release.process_release == RELEASE_PARTIAL
    ):
        return 1
    return 0


def register_sublane_hibernate_parser(sublane_sub: Any) -> None:
    """Register ``sublane hibernate`` outside the at-ceiling core CLI module.

    Mirrors the sibling ``register_sublane_resume_parser`` placement (the core CLI module
    is at the module-health ceiling), keeping the hibernate parser next to its use case
    (Redmine #13967). Includes the early-hibernate park-basis flags (item 1) and the
    project-gateway binding / exact-generation flags (Redmine #13811).
    """
    from mozyo_bridge.application.cli_common import add_repo_option

    sublane_hibernate = sublane_sub.add_parser(
        "hibernate",
        help=(
            "Redmine #13682: release an OPEN lane's managed gateway/worker processes "
            "while preserving its worktree / branch / unpublished commits / lane metadata "
            "/ durable callback route (tombstone-free — never closes the issue, removes a "
            "worktree, or deletes a branch). Fail-closed preflight (lane actively owns the "
            "issue; an affirmative park basis — dependency park or early hibernate; no "
            "callback/review/integration due (owner approval is required only for a "
            "dependency park, not early hibernate); no pending composer; no work in "
            "flight; a dirty worktree needs a boundary journal). Not an idle-timeout kill. "
            "Default is preflight only; --execute performs the hibernate. Exits non-zero "
            "when blocked. Resume with `sublane resume`."
        ),
    )
    sublane_hibernate.add_argument(
        "--issue", required=True, help="Redmine issue id the lane owns (stays open)"
    )
    sublane_hibernate.add_argument(
        "--lane",
        required=True,
        help="Lane label to hibernate (e.g. issue_<id>_<slug>)",
    )
    sublane_hibernate.add_argument(
        "--journal",
        required=True,
        help="Redmine journal id that authorizes the hibernate (durable anchor)",
    )
    sublane_hibernate.add_argument(
        "--project-scope",
        dest="project_scope",
        default="",
        help=(
            "Redmine #13811: the canonical full project scope of a PROJECT-GATEWAY lane "
            "(binding_kind=project_gateway, no issue). When set, the lane is identified by "
            "this scope and --issue names only the decision anchor's issue, and the "
            "action-time exact-generation / attestation fences apply. Omit for an "
            "issue-owned lane (the default, unchanged)."
        ),
    )
    sublane_hibernate.add_argument(
        "--expected-lane-generation",
        dest="expected_lane_generation",
        default="",
        help=(
            "Redmine #13811: the approved lane_generation asserted from the durable Redmine "
            "approval. Required with --project-scope; must equal the row's current "
            "generation, so a stale approval from a superseded incarnation "
            "(retire + open_next_generation) cannot re-bind to the current generation. "
            "Ignored for an issue-owned lane."
        ),
    )
    sublane_hibernate.add_argument(
        "--expected-revision",
        dest="expected_revision",
        default="",
        help=(
            "Redmine #13811: the approved lifecycle revision asserted from the durable "
            "Redmine approval. Required with --project-scope; the fresh active->hibernated "
            "CAS is bound to it, so an approval whose process authority advanced within the "
            "same generation (pin repair / replacement / decision update) fails closed "
            "pre-CAS. Ignored for an issue-owned lane."
        ),
    )
    # Durable-record invariants the operator asserts from the Redmine record (each
    # defaults to unsatisfied so an omitted flag fails closed).
    for _opt, _dest, _help in (
        ("--explicitly-parked", "explicitly_parked",
         "The issue is open and explicitly parked/blocked (dependency park basis)."),
        ("--callbacks-drained", "callbacks_drained",
         "The lane owes no outstanding coordinator callback."),
        ("--no-review-pending", "no_review_pending",
         "The lane has no review awaiting a result."),
        ("--no-owner-approval-pending", "no_owner_approval_pending",
         "The lane has no owner close approval pending. Required for a dependency park; "
         "NOT required for early hibernate (owner approval stays on the coordinator path)."),
        ("--no-integration-pending", "no_integration_pending",
         "The lane has no integration disposition pending."),
        ("--no-pending-prompt", "no_pending_prompt",
         "The lane has no composer input pending."),
        ("--not-working", "not_working", "The lane has no work in flight."),
        ("--worktree-clean", "worktree_clean",
         "The lane's worktree has no uncommitted diff (no boundary journal needed)."),
        ("--boundary-recorded", "boundary_recorded",
         "A boundary journal capturing the dirty worktree's diff / resume next-action "
         "is recorded (required when the worktree is not clean)."),
        # Early-hibernate park basis (Redmine #13967 item 1): the alternative to
        # --explicitly-parked for a review-approved + staging-integrated feature lane
        # whose dogfood execution/evidence is delegated to the dedicated release issue
        # (close authority stays with the coordinator). All five must hold to qualify
        # (each defaults unsatisfied -> fail closed).
        ("--review-approved", "review_approved",
         "Early hibernate: the same-lane Review Gate is approved with no open findings."),
        ("--staging-integrated", "staging_integrated",
         "Early hibernate: the coordinator staging integration is recorded (merged / "
         "patch-equivalent to the staging branch)."),
        ("--required-ci-green", "required_ci_green",
         "Early hibernate: the required CI for the integrated commits is green."),
        ("--dogfood-delegated", "dogfood_delegated",
         "Early hibernate: TestPyPI / installed dogfood execution/evidence is delegated to "
         "the dedicated release issue via a durable park/delegation record (close authority "
         "and owner close approval stay with the coordinator, not delegated)."),
        ("--commits-pushed", "commits_pushed",
         "Early hibernate: the lane's commits are pushed / origin-reachable (unpushed "
         "fails closed — an early hibernate presupposes integrated work)."),
    ):
        sublane_hibernate.add_argument(
            _opt, dest=_dest, action="store_true", help=_help
        )
    sublane_hibernate.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help=(
            "Perform the hibernate: CAS the disposition (active->hibernated) and release "
            "the lane's managed processes. Without it this is preflight only (no mutation)."
        ),
    )
    add_repo_option(sublane_hibernate)
    sublane_hibernate.add_argument(
        "--json", action="store_true", help="Emit structured JSON output"
    )
    sublane_hibernate.set_defaults(func=cmd_sublane_hibernate)


__all__ = (
    "cmd_sublane_hibernate",
    "format_hibernate_text",
    "register_sublane_hibernate_parser",
)
