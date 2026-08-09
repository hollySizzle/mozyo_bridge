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
            "when --review-generation-json or --review-exemption-json is supplied (those MEASURE "
            "admissibility at action-time). Do NOT pass it for a review-exempt lane: there is no "
            "review generation to be approved, so the assert would be false — use "
            "--review-exemption-json instead (#14539)."
        ),
    )
    sublane_retire.add_argument(
        "--review-exemption-json",
        dest="review_exemption_json",
        default=None,
        help=(
            "#14539: path to the issue's durable journals "
            "{issue, journals:[{journal_id, notes}]}. When supplied, the retire RE-VERIFIES at "
            "action-time that a `codex_direct_edit` gate with `follow_up_review: false` is in "
            "force AND the issue records Close AND the integration disposition is complete — the "
            "three facts that let a review-exempt lane pass the latest-generation fence without a "
            "false --latest-generation-admissible assert. The observation's `issue` MUST literal "
            "exact-match --issue (evidence from another issue never unlocks this fence), the gate's "
            "`allowed_paths` must cover the record's declared changed_paths, and a review round "
            "opened AFTER the exemption re-owes the review. Fail-closed on an unreadable / "
            "malformed file or on any missing fact. The integration half is proved by the "
            "lane-enveloped strict evidence on the CURRENT disposition journal, whose reviewed "
            "source head must be the covered commit, whose envelope must match the retire "
            "TARGET's own lifecycle row (workspace / lane / generation, measured from durable "
            "state — never from a flag) and --integration-branch, and whose issuer must resolve "
            "to the coordinator under the Hibernate Evidence Marker Contract; a legacy "
            "lane-unbound note is valid for the glance but never auto-admits a retire."
        ),
    )
    sublane_retire.add_argument(
        "--no-change-review-waiver",
        dest="no_change_review_waiver",
        action="store_true",
        help=(
            "#14695: re-verify at action-time that this lane produced NO repository change and "
            "carries a direct-owner `no_change_review_waiver`. **THIS ROUTE CURRENTLY ADMITS "
            "NOTHING** and always refuses with `waiver_writer_authority_unresolvable` (review "
            "j#93776 finding 1): this record system cannot establish WHO wrote a journal — every "
            "role posts under one source-system account and the issuer resolution is a policy "
            "binding that takes no author input — so a lane worker could write its own waiver. "
            "The issue's Acceptance sanctions this typed refusal until a writer/receipt authority "
            "bound to an actual coordinator action is ruled on. Every other check below is live "
            "and reports its own reason first, so this flag is still useful for diagnosing a "
            "record; it just cannot unlock a retire. The intended contract, for when it can: "
            "an investigation with no review generation because it changed nothing (#14613 "
            "j#93256 / j#93262). Deliberately a bare opt-in and NOT a "
            "JSON path: the issue's full journal history is read LIVE over the credential-gated "
            "Redmine read, because this route's premise is NEGATIVE (no commit, no changed_paths, "
            "no change-bearing gate, no integration disposition anywhere in the record) and a "
            "caller-supplied file would satisfy a negative claim by omission alone. Admits only "
            "when ALL of: one canonical `no_change_review_waiver` marker whose issue and whose "
            "workspace/lane/lane_generation envelope exact-match the retire TARGET's own "
            "lifecycle row (measured from durable state, never from a flag); its writer resolves "
            "to the coordinator under the gate's own ruling; the latest gate is Close; no review "
            "round is newer than the waiver; the record declares zero change; no recognized "
            "durable fact names a hard carve-out surface (release / production verification / "
            "credential / destructive / migration / external effect) and the gate inventory "
            "actually resolved; --callbacks-drained; and the live repository still agrees — the "
            "branch head literal-equals the waiver's head, the branch carries 0 commits over "
            "--integration-branch, and the --worktree checkout is clean. Fail-closed on every "
            "gap, including unconfigured credentials and an unmeasurable repository. Never pass "
            "--latest-generation-admissible for such a lane: there is no review generation, so "
            "the assert would be false."
        ),
    )
    sublane_retire.add_argument(
        "--superseded-failure-terminal",
        dest="superseded_failure_terminal",
        action="store_true",
        help=(
            "#14755: re-verify at action-time that this lane's latest review round FAILED and has "
            "been durably terminalized as superseded, so it can converge to retired WITHOUT being "
            "read as an approval. For a round that concluded `changes_requested` the ordinary "
            "generation fence can only ever refuse, and the two escapes from that — asserting "
            "--latest-generation-admissible about a failed round, or borrowing the successor "
            "issue's approval for this lane — are both false (reproduction: #14577 j#93648 "
            "changes_requested, terminal j#93757, blocked retires j#93759 / j#94006 / j#94319). "
            "Nothing in this route ever reads `changes_requested` as approved: it REQUIRES the "
            "failure. Deliberately a bare opt-in and NOT a JSON path, because its premise is a "
            "CORRELATION across two issues and every part of it is falsifiable by dropping a "
            "journal, so both issues' full histories are read LIVE over the credential-gated "
            "Redmine read. Admits only when ALL of: one canonical `superseded_failure` marker "
            "whose issue, whose workspace/lane/lane_generation envelope (exact-matched against "
            "the retire TARGET's own lifecycle row, measured from durable state, never from a "
            "flag) and whose integration_branch is the repository's COMMITTED "
            "`sublane_integration.integration_branch` — which --integration-branch must also name, "
            "because the live measurement is taken against it and a caller free to choose it "
            "could point it at the lane's own branch and make the 0-commit conjunct vacuous; a "
            "config declaring none supplies no expectation and refuses; no review round stands "
            "at-or-after that declaration; the latest gate is Close; --callbacks-drained; the "
            "record's NEWEST review round is the one the declaration names AND it concluded "
            "`changes_requested`; the governed `review_finding_verdict` gate the declaration "
            "names is the latest one, targets that round, and records `accepted` for every "
            "finding; the named successor is a DIFFERENT issue whose own record carries a "
            "`superseded_failure_successor` acknowledgement naming this issue and this round, and "
            "whose newest round is the approval it names and which is itself closed; and the live "
            "repository still agrees ABOUT THIS LANE — --branch is the declaration's own lane (any "
            "checkout sitting on the head would otherwise do, which matters precisely when the "
            "lane head is already the integration head), its head literal-equals the "
            "declaration's head, the --worktree checkout is clean, and it carries 0 commits over "
            "--integration-branch. That last conjunct is what BOUNDS this route: a lane still "
            "holding unintegrated work never reaches the terminal, so admitting drains a process "
            "without integrating anything or minting any approval. It does NOT establish who "
            "wrote the declaration — no record in this workspace can (ruling #14219 j#86718) — "
            "and it makes no issuer claim rather than manufacturing a ruling anchor that decided "
            "no writer contract. Fail-closed on every gap, including unconfigured credentials and "
            "an unmeasurable repository."
        ),
    )
    sublane_retire.add_argument(
        "--superseded-audit-failure-terminal",
        dest="superseded_audit_failure_terminal",
        action="store_true",
        help=(
            "#15166: re-verify at action-time that this lane has NO formal Review Gate at all, "
            "that its round-1 verdict was recorded by an INDEPENDENT AUDIT journal, and that the "
            "acceptance it did not reach was obtained by a successor issue that acknowledges the "
            "supersession — so it can converge to retired without any approval being asserted, "
            "borrowed or invented. Reproduction #15164: `review_request` was never posted so no "
            "`## Gate: review` exists (j#101792 says so in as many words), the successor #15165 "
            "was approved (j#101810) and closed, both issues are task_closed, the lane never "
            "committed — and the retire still refused permanently with `stale_review_generation` "
            "(j#101825), because the ordinary fence reads a review generation this lane does not "
            "have and --superseded-failure-terminal REQUIRES a round that concluded "
            "`changes_requested`. Deliberately a bare opt-in and NOT a JSON path: its premise is a "
            "CORRELATION across two issues AND two NEGATIVE claims over the whole record (no "
            "review round anywhere, no repository change declared anywhere), which a "
            "caller-supplied file would satisfy by omission alone, so both histories AND both "
            "issues' current tracker status are read LIVE over the credential-gated Redmine read. "
            "**THE AUTHORITY IS A RECORDED COORDINATOR DECISION**, written by "
            "`mozyo-bridge sublane audit-failure-terminal record`. A lane with no decision is "
            "refused with `no_recorded_coordinator_terminal_decision`, and a decision that does "
            "not name what this retire measured is refused as drift, whatever the records say. "
            "Three rounds tried to derive that binding from the journals and each was refuted by "
            "measurement: mutual acknowledgement (review j#101880 finding 1 — one "
            "unauthenticatable writer can place both halves), head coverage (review j#101909 "
            "finding 1 — on a zero-change lane the lane head IS the integration head, so every "
            "unrelated approved issue on that base shares it), and an in-package enumeration "
            "(review j#102074 finding 1 / scope decision j#102081). The binding is a coordinator "
            "judgement, and `managed-state-model.md` places a judgement taken at a mozyo command "
            "boundary in `desired_state`, whose authority is mozyo-owned persisted state — so it "
            "is recorded there, where no sequence of Redmine journal writes can reach it. Single "
            "use is the lifecycle revision: the decision is bound to the row's exact revision, "
            "which every retire that mutates the row advances. "
            "On top of that decision it admits only when ALL of: one canonical "
            "`superseded_audit_failure` marker whose issue, whose workspace/lane/lane_generation "
            "envelope (exact-matched against the retire TARGET's own lifecycle row, measured from "
            "durable state, never from a flag) and whose integration_branch is the repository's "
            "COMMITTED `sublane_integration.integration_branch` — which --integration-branch must "
            "also name, because the live measurement is taken against it; the issue records ZERO "
            "formal review rounds (a round that exists belongs to the ordinary fence or to "
            "--superseded-failure-terminal, and refusing here is what stops this route becoming a "
            "second way past a review that did happen); the latest gate is Close and NO recognized "
            "lifecycle gate stands at-or-after the declaration (so the declaration is written "
            "after the Close, in its own journal, and a re-opened lane is caught); the TRACKER "
            "currently reports BOTH issues closed, from a fresh action-time read — a Close gate "
            "journal is the lane's own belief and a status-only reopen adds no `## Gate:` note, so "
            "an unreadable or open status refuses (review j#101880 finding 2); "
            "--callbacks-drained; the journal named as the audit record EXISTS in this issue's "
            "history, is NOT a recognized lifecycle gate, and is OLDER than the declaration; the "
            "record declares ZERO repository change (required beside the live check because a "
            "zero ahead-count alone is also what already-merged work looks like); the named "
            "successor is a DIFFERENT issue whose own record carries a "
            "`superseded_audit_failure_successor` acknowledgement naming this issue and this audit "
            "journal, and whose newest round is the approval it names and which is itself closed; "
            "that approval's reviewed head literal-equals the decided head; and the live "
            "repository still agrees ABOUT THIS LANE — --branch is the declaration's "
            "own lane, its head literal-equals the declaration's head, the --worktree checkout is "
            "clean, and it carries 0 commits over --integration-branch. The zero-change and "
            "live-zero conjuncts BOUND the route: there is no state other than the reviewed one, "
            "so admitting drains a process without integrating anything or minting any approval. "
            "It does NOT establish that the audit concluded a failure (that is prose, not a "
            "governed surface), it does NOT assert that THIS issue passed a review (the reviewed "
            "head says which commit state was examined, never whose acceptance was met), and "
            "it does NOT establish who wrote anything — no record in this workspace can (ruling "
            "#14219 j#86718) — the decision moves the authority off the journal surface rather "
            "than closing that gap. "
            "Fail-closed on every gap, including unconfigured credentials and an unmeasurable "
            "repository. Never pass --latest-generation-admissible for such a lane: there is no "
            "review generation, so the assert would be false."
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
            "coordinator patch_equivalent integration verified from the exact Redmine journal via "
            "--integration-journal, Redmine #14066), moves it directly to the terminal "
            "`retired` disposition via a bounded CAS, preserving the row's declared pins and "
            "worktree identity. Launches / closes / resumes NO process; removes no worktree / "
            "branch. Mutually exclusive with --execute, --migrate-hibernated-legacy and "
            "--reconcile-hibernated-live (passing more than one is a zero-write error)."
        ),
    )
    sublane_retire.add_argument(
        "--retire-active-live-zero",
        dest="retire_active_live_zero",
        action="store_true",
        help=(
            "Redmine #14242: metadata-only TERMINAL retire for an ACTIVE bound owner row whose "
            "managed pair is already positively gone — the #14222 j#85208 gap where the issue "
            "and its children are closed, the head is integrated and the worktree is clean, but "
            "the #13754 guarded close returns a permanent `zero_close_unproven` (nothing to "
            "close, yet the row is not `retired`) and --retire-hibernated-bound refuses with "
            "`not_hibernated_bound_state` because its CAS requires hibernated + released. "
            "Unlike that surface there is NO durable release witness to pair with (an active row "
            "never requested one), so the live-inventory zero read is the only liveness "
            "authority and every ambiguity is refused: an unreadable inventory, a duplicate "
            "canonical slot, a locator-less row the liveness contract does not positively call "
            "dead, and any foreign / unexpected occupant each fail closed zero-write. Requires "
            "the preflight to permit retirement AND --worktree to attest against the row's "
            "recorded canonical binding AND the row to be active + issue-bound + owning --issue "
            "AND --branch integrated (literal ancestor, or a #14066 patch_equivalent "
            "integration verified via --integration-journal). The CAS is fenced on the exact "
            "revision the zero read was measured against, so a pair relaunched in between loses "
            "rather than being clobbered. Launches / closes / resumes NO process; removes no "
            "worktree / branch. Duplicate replay is idempotent. Mutually exclusive with "
            "--execute, --migrate-hibernated-legacy, --reconcile-hibernated-live and "
            "--retire-hibernated-bound (passing more than one is a zero-write error)."
        ),
    )
    sublane_retire.add_argument(
        "--retire-active-unbound-live-zero",
        dest="retire_active_unbound_live_zero",
        action="store_true",
        help=(
            "Redmine #14499: metadata-only TERMINAL retire for an ACTIVE row that records NO "
            "canonical worktree binding (an empty worktree_identity) and whose managed pair is "
            "already positively gone — the #14456 j#87973 shape no rail could converge: the "
            "guarded close returns `worktree_binding_unverified` (nothing to attest), "
            "--retire-active-live-zero refuses an EMPTY binding by construction, and "
            "--migrate-hibernated-legacy / --reconcile-hibernated-live / "
            "--retire-hibernated-bound all require a hibernated row. With no binding to attest "
            "and no release witness, its identity fence is the caller-declared "
            "--expect-lane-generation + --expect-lane-revision (both mandatory; read them from "
            "`sublane reboot-audit`), so a lane re-incarnated between the read and the write "
            "loses the CAS rather than being terminalized on a stale reading. Requires the "
            "preflight to permit retirement AND the row to be active + issue-bound + owning "
            "--issue + EMPTY-bound AND --branch integrated (literal ancestor, or a #14066 "
            "patch_equivalent integration verified via --integration-journal), and takes the "
            "same launch-exclusion lock and live-zero fences as #14242. --worktree is NOT "
            "required and is never attested here (it is used only to widen the live-zero scan "
            "to a pre-#13377 legacy twin unit). Launches / closes / resumes NO process; removes "
            "no worktree, branch or commit. Duplicate replay is idempotent. Mutually exclusive "
            "with every other retire intent (passing more than one is a zero-write error)."
        ),
    )
    sublane_retire.add_argument(
        "--retire-hibernated-unbound-live-zero",
        dest="retire_hibernated_unbound_live_zero",
        action="store_true",
        help=(
            "Redmine #14716: metadata-only TERMINAL retire for a HIBERNATED + RELEASED "
            "issue-bound row with an EMPTY canonical worktree binding and a positively "
            "absent managed pair. Requires exact generation/revision, lane metadata branch, "
            "integrated head, one fresh Redmine snapshot proving the exact issue closed and "
            "--journal present, exclusive launch exclusion, and live-zero. --worktree is "
            "optional and can only refuse on metadata mismatch; it never authorizes the "
            "write. Restores/removes no checkout and changes no Git ref. Mutually exclusive "
            "with every other retire intent."
        ),
    )
    sublane_retire.add_argument(
        "--expect-lane-generation",
        dest="expect_lane_generation",
        type=int,
        default=0,
        help=(
            "Redmine #14499: the exact positive lane_generation the caller measured the "
            "live-zero read against. Mandatory with --retire-active-unbound-live-zero and "
            "--retire-hibernated-unbound-live-zero (it replaces the worktree attestation "
            "those surfaces cannot perform); ignored by every other intent."
        ),
    )
    sublane_retire.add_argument(
        "--expect-lane-revision",
        dest="expect_lane_revision",
        type=int,
        default=0,
        help=(
            "Redmine #14499: the exact positive lifecycle revision the caller measured the "
            "live-zero read against. Mandatory with both unbound live-zero retire intents; "
            "ignored by every other intent."
        ),
    )
    sublane_retire.add_argument(
        "--integration-journal",
        dest="integration_journal",
        default=None,
        help=(
            "Redmine #14066: the Redmine journal id (on --issue) carrying the coordinator's "
            "durable `patch_equivalent` integration disposition — a fenced "
            "`mozyo-patch-equivalent-integration` JSON block {issue, lane, branch, "
            "integration_branch, source_head, integration_head, origin_reachable, "
            "commit_map:[{source,integration,patch_id}]}. Used ONLY with --retire-hibernated-bound "
            "and ONLY when --branch is not a literal ancestor of --integration-branch: the retire "
            "fresh-reads that EXACT journal over the credential-gated Redmine read (the durable "
            "authority — never a caller-supplied file), RECOMPUTES the stable patch-ids and "
            "origin reachability (against origin/<integration-branch>) from real git, and "
            "terminalizes only when every mapped cherry-pick is proven patch-equivalent and the "
            "recorded heads match the current branches. Unconfigured credentials / unreadable "
            "Redmine / journal-not-found / missing / ambiguous / malformed / stale / mismatched "
            "evidence all fail closed (zero-write). The literal-ancestor path ignores it entirely."
        ),
    )
    add_repo_option(sublane_retire)
    add_lifecycle_json(sublane_retire)
    sublane_retire.set_defaults(func=cmd_sublane_retire)


__all__ = ("register_sublane_retire",)
