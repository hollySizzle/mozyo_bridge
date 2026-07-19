"""``sublane retire`` CLI parser (Redmine #13754).

Feature-local parser registration, following the convention the other bounded contexts
already use (``cli_agents`` / ``cli_handoff`` / ``cli_release`` / ``cli_module_health``):
a command's parser lives with the feature that owns it, not in the shared ``cli_core``
assembly site. ``cli_core`` composes it by calling :func:`register_sublane_retire`.

Moved here rather than allowlisted: ``cli_core`` sat two lines under the module-health
threshold, so *any* new sublane flag tripped the ``new_oversized`` gate. The gate's
remedy is to reduce, and the retire parser's home is the retire feature. Pure relocation
— the parser, its flags, and their semantics are unchanged; the only new surface is
``--journal`` (the durable anchor the #13754 retirement disposition is recorded with).
"""

from __future__ import annotations

import argparse
from typing import Callable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
    cmd_sublane_retire,
)


def register_sublane_retire(
    sublane_sub,
    *,
    add_repo_option: Callable[[argparse.ArgumentParser], None],
    add_lifecycle_json: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register ``sublane retire`` on the ``sublane`` subparser group.

    The two shared option helpers stay owned by ``cli_core`` (every subcommand shares
    them) and are injected, so this module adds no import cycle back into the CLI core.
    """
    sublane_retire = sublane_sub.add_parser(
        "retire",
        help=(
            "Fail-closed retire preflight: evaluate the retire decision from git "
            "probes + durable-record invariants and emit the verdict + journal + "
            "retirement runbook. Does NOT actuate worktree remove / branch delete "
            "(gated); never deletes remote branches. Exits non-zero when retirement "
            "is blocked — and, under --execute, also when the guarded close could not "
            "prove it retired the lane (unresolved target identity, unreadable "
            "inventory, a failed close, or an unproven zero-close: Redmine #13754)."
        ),
    )
    sublane_retire.add_argument("--issue", required=True, help="Redmine issue id")
    sublane_retire.add_argument(
        "--journal",
        default=None,
        help=(
            "Redmine journal id of the retirement decision: the durable anchor the "
            "lane's `retired` lifecycle disposition is recorded with under --execute "
            "(Redmine #13754). Without it the panes still close but the retirement is "
            "not durably recorded, so a later zero-close re-run fails closed."
        ),
    )
    sublane_retire.add_argument(
        "--lane-label",
        dest="lane_label",
        required=True,
        help="Lane label to retire (e.g. issue_<id>_<slug>)",
    )
    sublane_retire.add_argument(
        "--worktree", default=None, help="Worktree path to include in the runbook"
    )
    sublane_retire.add_argument(
        "--branch", default=None, help="Local branch to include in the runbook"
    )
    sublane_retire.add_argument(
        "--integration-branch",
        dest="integration_branch",
        default=None,
        help="Integration branch name (recorded in the durable journal)",
    )
    # Durable-record invariants the operator asserts (each defaults to unsatisfied
    # so an omitted flag fails closed).
    sublane_retire.add_argument(
        "--issue-closed",
        dest="issue_closed",
        action="store_true",
        help=(
            "The lane's Redmine issue is durably closed under the close contract that "
            "applies to its issue type (a child Task/Test/Bug via task_close; a US / "
            "standalone issue via an owner_close_approval-backed close). Redmine #13602 "
            "(Option A): routine green-preflight retirement is coordinator authority and "
            "takes no separate --owner-approved flag regardless of which close contract "
            "applied — retire actuation never re-collects the owner close approval."
        ),
    )
    sublane_retire.add_argument(
        "--callbacks-drained",
        dest="callbacks_drained",
        action="store_true",
        help="No outstanding coordinator callback is owed.",
    )
    sublane_retire.add_argument(
        "--verified",
        dest="verified",
        action="store_true",
        help="The lane's verification (tests / checks) passed.",
    )
    sublane_retire.add_argument(
        "--durable-record",
        dest="durable_record",
        action="store_true",
        help="The durable retire record / anchor is present.",
    )
    sublane_retire.add_argument(
        "--target-identity-known",
        dest="target_identity_known",
        action="store_true",
        help="The lane / worktree / pane target is positively resolved.",
    )
    sublane_retire.add_argument(
        "--latest-generation-admissible",
        dest="latest_generation_admissible",
        action="store_true",
        help=(
            "#13518 R2-F7 / R3-F2: assert (from the durable review journals) that the LATEST review "
            "generation is approved AND carries no unresolved blocking finding. Fail-closed when "
            "unset: the actual retire/integration no longer default-admits a stale approval. Ignored "
            "when --review-generation-json is supplied (that MEASURES it at action-time)."
        ),
    )
    sublane_retire.add_argument(
        "--review-generation-json",
        dest="review_generation_json",
        default=None,
        help=(
            "#13518 R3-F2: path to a coordinator-produced durable review observation "
            "{issue, review_request_journal, target_head, decisions:[{kind,seq,blocking,disposition,"
            "journal_id}]}. When supplied, latest-generation admissibility is MEASURED at action-time "
            "via the review-generation fence (an unreadable / malformed file fails closed)."
        ),
    )
    sublane_retire.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help=(
            "Redmine #13331: under backend: herdr, and only when the preflight permits "
            "retirement, close the lane workspace's managed gateway/worker agents "
            "(mzb1 default-lane codex/claude). Never removes a worktree or deletes a "
            "branch (still runbook); never closes a foreign agent. No-op under tmux."
        ),
    )
    sublane_retire.add_argument(
        "--migrate-hibernated-legacy",
        dest="migrate_hibernated_legacy",
        action="store_true",
        help=(
            "Redmine #13841: metadata-only migration for a hibernated / released LEGACY "
            "owner row (empty worktree binding) whose live pair is gone. Only when the "
            "preflight permits retirement AND the durable row is hibernated + released + "
            "empty-worktree + owns --issue AND the live inventory shows zero managed slots "
            "AND --branch is integrated, moves it directly to the terminal `retired` "
            "disposition via a bounded CAS. Launches / closes / resumes NO process; removes "
            "no worktree / branch. Mutually exclusive with --execute: passing both is a "
            "zero-write error (the migration never closes a pane)."
        ),
    )
    sublane_retire.add_argument(
        "--reconcile-hibernated-live",
        dest="reconcile_hibernated_live",
        action="store_true",
        help=(
            "Redmine #13842: reconcile a hibernated / released LEGACY owner row (empty "
            "worktree binding) whose exact managed pair is nonetheless observed LIVE — the "
            "#13756 j#79188 contradiction the #13841 live-zero migration, the #13754 guarded "
            "close, and the #13809 backfill all leave with no convergence path. Only when the "
            "preflight permits retirement AND the exact live pair is unique + idle/turn-ended "
            "+ settled + generation-bound attested AND --branch is integrated, it re-establishes "
            "the missing worktree + process binding via a bounded CAS, then hands off to the "
            "#13754 guarded close to close the pair and record the terminal `retired` "
            "disposition (one replayable flow). Launches / resumes NO process; removes no "
            "worktree / branch. Mutually exclusive with --execute and "
            "--migrate-hibernated-legacy (passing more than one is a zero-write error)."
        ),
    )
    sublane_retire.add_argument(
        "--retire-hibernated-bound",
        dest="retire_hibernated_bound",
        action="store_true",
        help=(
            "Redmine #13845: metadata-only TERMINAL retire for a hibernated / released BOUND "
            "owner row (non-empty worktree binding) whose live pair is already gone — the "
            "#13810 j#79416 gap the #13754 guarded close leaves as a permanent "
            "`zero_close_unproven` (nothing to close, yet the durable row is not `retired`), "
            "and that the #13841 migration / #13842 reconcile both refuse because they require "
            "an EMPTY binding. Only when the preflight permits retirement AND --worktree "
            "attests against the row's recorded canonical binding AND the durable row is "
            "hibernated + released + owns --issue AND the live inventory shows every expected "
            "managed slot absent AND no foreign / unexpected provider occupies the lane unit "
            "AND --branch is integrated (a literal ancestor of --integration-branch, OR a "
            "coordinator patch_equivalent integration verified via --integration-disposition-json, "
            "Redmine #14066), moves it directly to the terminal "
            "`retired` disposition via a bounded CAS, preserving the row's declared pins and "
            "worktree identity. Launches / closes / resumes NO process; removes no worktree / "
            "branch. Mutually exclusive with --execute, --migrate-hibernated-legacy and "
            "--reconcile-hibernated-live (passing more than one is a zero-write error)."
        ),
    )
    sublane_retire.add_argument(
        "--integration-disposition-json",
        dest="integration_disposition_json",
        default=None,
        help=(
            "Redmine #14066: path to the coordinator's durable `patch_equivalent` integration "
            "disposition, captured from the exact integration journal "
            "{issue, lane, branch, integration_branch, source_head, integration_head, origin_ref, "
            "origin_reachable, journal_id, commit_map:[{source,integration,patch_id}]}. Used ONLY "
            "with --retire-hibernated-bound and ONLY when --branch is not a literal ancestor of "
            "--integration-branch: the retire re-reads the disposition and RECOMPUTES the stable "
            "patch-ids + origin reachability from real git at action-time, terminalizing only when "
            "every mapped cherry-pick is proven patch-equivalent and the recorded source/"
            "integration heads match the current branches. Missing / malformed / stale / "
            "mismatched evidence fails closed (zero-write). The literal-ancestor path ignores it."
        ),
    )
    add_repo_option(sublane_retire)
    add_lifecycle_json(sublane_retire)
    sublane_retire.set_defaults(func=cmd_sublane_retire)


__all__ = ("register_sublane_retire",)
