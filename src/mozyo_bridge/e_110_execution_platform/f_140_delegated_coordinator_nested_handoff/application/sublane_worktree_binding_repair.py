"""``mozyo-bridge sublane repair-worktree-binding`` (Redmine #14475, review j#88490).

The public, metadata-only rail that converges the one shape #14475 could diagnose but nothing
could fix: a **hibernated / released** lifecycle row whose ``declared_slots`` are present but
whose canonical ``worktree_identity`` is EMPTY.

Why it needs its own command: the #13809 ``backfill_active_binding`` the create path uses is
``active``-only, so on a hibernated row it is ``unexpected_state`` zero-write. That made the
``recover-pair`` / ``recover-gateway`` blocker runbook ("re-run the lane's own declaration
surface") true for an active lane and **false for a hibernated one** — a fence that names a
recovery nobody can perform. This command is that recovery.

It writes one lifecycle PAYLOAD field — ``worktree_identity`` — plus the decision /
revision / updated_at audit metadata every CAS in this component stamps, through
:meth:`...LaneWorktreeBindingRepairStore.repair_hibernated_worktree_binding` and touches no
process: no launch, no close, no resume, no send, no worktree or branch mutation. The lane
stays hibernated; ``sublane recover-pair`` remains the surface that acts on it afterwards.

Before writing anything it demands **positive evidence that the named worktree really is this
lane's**, because the row itself carries no token to compare against (that is the defect):

* the path must resolve to a live git checkout, and be that checkout's **root** (a
  subdirectory answers the same branch query but derives a different canonical token);
* it must resolve to the **same workspace** as the lane record, through
  :func:`...sublane_herdr_projection.repo_scope_workspace_id` — the canonical resolver that
  makes a linked worktree inherit its main checkout's identity. Without this axis a branch
  NAME would be treated as an identity, and any repository holding a same-named ``issue_…``
  branch could have its token written into this workspace's lane row (review j#88493);
* its current branch must equal the lane id — the same branch axis
  :meth:`...LiveStaleWorkerRecoveryOps.lane_authority_reason` re-joins before any owed launch.

A ``--worktree`` that is merely asserted on the command line is never trusted: without that
evidence the token is not derived and nothing is written.

The repair's own ``--journal`` becomes the row's decision anchor (review j#88493 item 2),
matching every sibling in this component: a lifecycle row must always name the durable record
that put it in its CURRENT state, never an inherited one from an earlier write (the R1-F5
rule ``transition_disposition`` states). What stays untouched is the **issue** binding, the
pins, the disposition and the generation — the WORKTREE binding is precisely what this
command changes (empty -> the canonical token), so calling "the binding" invariant would
contradict the command's own purpose (review j#88495).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.core.state.lane_worktree_binding_signature import (
    SIGNATURE_BINDING_KIND,
    SIGNATURE_INVALID_PINS,
    SIGNATURE_MISSING_PINS,
    SIGNATURE_NOT_HIBERNATED,
    SIGNATURE_OK,
    SIGNATURE_PINS_NOT_CANONICAL,
    SIGNATURE_PROJECT_SCOPE,
    SIGNATURE_RELEASE_NOT_SETTLED,
    SIGNATURE_REPLACEMENT_IN_FLIGHT,
    SIGNATURE_WRONG_ISSUE,
    classify_repair_signature,
)

#: The repair ran and the row now carries the canonical token (or already did — byte-equal
#: replay is an idempotent success).
REPAIR_APPLIED = "repaired"
#: Read-only preflight (the default): the target matches and ``--execute`` would write.
REPAIR_PREFLIGHT = "preflight"
#: Refused before any write. ``detail`` names the exact fence.
REPAIR_BLOCKED = "blocked"

# -- fail-closed blocker vocabulary (closed, secret-safe) -----------------------

BLOCK_IDENTITY_INCOMPLETE = "identity_or_decision_incomplete"
BLOCK_STORE_UNREADABLE = "lifecycle_store_unreadable"
BLOCK_ROW_ABSENT = "lifecycle_row_absent"
BLOCK_NOT_HIBERNATED = "lane_not_hibernated"
BLOCK_WRONG_ISSUE = "lane_owns_a_different_issue"
BLOCK_ALREADY_BOUND = "worktree_binding_already_present"
BLOCK_MISSING_PINS = "hibernated_record_missing_pins"
#: The row is a project-gateway binding; this surface repairs an issue-bound lane
#: (review j#88505 F2 — a store predicate the preflight must project, not discover at execute).
BLOCK_PROJECT_SCOPE = "lane_owns_a_project_scope"
#: The row's ``binding_kind`` is not ``issue``. The store checks this as an INDEPENDENT axis
#: from ``project_scope``, so a malformed / legacy row (project-gateway kind with an empty
#: scope) would otherwise read green here and be refused at execute (review j#88512).
BLOCK_BINDING_KIND = "lane_binding_kind_is_not_issue"
#: The row's declared pins do not survive the store's own ``validate_declared_slots``
#: (duplicate stable identities, malformed pins). The preflight applies the SAME pure
#: validator the CAS applies, rather than only checking non-emptiness (review j#88513 F2).
BLOCK_INVALID_PINS = "declared_pins_fail_validation"
#: The pins validate but are stored non-canonically, so the guarded CAS can never match them
#: (review j#88526 F2 — a raw-bytes axis a normalizing preflight cannot see).
BLOCK_PINS_NOT_CANONICAL = "declared_pins_are_not_canonically_encoded"
#: The lane's process release is not durably ``released`` (never requested, or requested /
#: partial and still in flight) — an actuator may be mutating its slots (review j#88505 F2).
BLOCK_RELEASE_NOT_SETTLED = "lane_process_release_not_settled"
#: A receiver replacement is in flight for this lane (review j#88505 F2).
BLOCK_REPLACEMENT_IN_FLIGHT = "lane_replacement_in_flight"
BLOCK_WORKTREE_UNREADABLE = "worktree_not_a_live_checkout"
#: ``--worktree`` names a SUBDIRECTORY of a checkout, not the worktree root. A subdir answers
#: the same branch query but derives a different canonical token (review j#88493).
BLOCK_WORKTREE_NOT_ROOT = "worktree_path_is_not_the_worktree_root"
#: ``--worktree`` resolves to a DIFFERENT workspace than the lane record's — a same-named
#: branch in another repository is not this lane (review j#88493).
BLOCK_FOREIGN_WORKSPACE = "worktree_belongs_to_a_foreign_workspace"
BLOCK_BRANCH_DRIFTED = "worktree_branch_is_not_the_lane_branch"
BLOCK_TOKEN_UNDERIVABLE = "worktree_token_underivable"
BLOCK_CAS_REFUSED = "repair_cas_refused"


#: The command-facing blocker for each shared signature axis. One entry per axis, so a new
#: axis in the classifier is a mapping error here rather than a silently missing fence.
_SIGNATURE_BLOCKERS = {
    SIGNATURE_NOT_HIBERNATED: BLOCK_NOT_HIBERNATED,
    SIGNATURE_BINDING_KIND: BLOCK_BINDING_KIND,
    SIGNATURE_WRONG_ISSUE: BLOCK_WRONG_ISSUE,
    SIGNATURE_PROJECT_SCOPE: BLOCK_PROJECT_SCOPE,
    SIGNATURE_MISSING_PINS: BLOCK_MISSING_PINS,
    SIGNATURE_INVALID_PINS: BLOCK_INVALID_PINS,
    SIGNATURE_PINS_NOT_CANONICAL: BLOCK_PINS_NOT_CANONICAL,
    SIGNATURE_RELEASE_NOT_SETTLED: BLOCK_RELEASE_NOT_SETTLED,
    SIGNATURE_REPLACEMENT_IN_FLIGHT: BLOCK_REPLACEMENT_IN_FLIGHT,
}

_SIGNATURE_DETAILS = {
    SIGNATURE_NOT_HIBERNATED: (
        "this surface repairs a HIBERNATED row; an active lane binds through its own "
        "declaration surface (sublane create self-heal)"
    ),
    SIGNATURE_BINDING_KIND: (
        "this surface repairs an ISSUE-bound lane; the row's binding kind is not 'issue'"
    ),
    SIGNATURE_WRONG_ISSUE: (
        "the row's stored issue is not exactly this issue (a different or padded value)"
    ),
    SIGNATURE_PROJECT_SCOPE: (
        "this surface repairs an ISSUE-bound lane; the row carries a project scope"
    ),
    SIGNATURE_MISSING_PINS: (
        "the row carries no declared pins; repair its pins first (sublane repair-pins)"
    ),
    SIGNATURE_INVALID_PINS: (
        "the row's declared pins do not pass validation (duplicate or malformed slots); "
        "repair its pins first (sublane repair-pins)"
    ),
    SIGNATURE_PINS_NOT_CANONICAL: (
        "the row's declared pins are stored in a non-canonical encoding, so the guarded CAS "
        "could never match them; repair its pins first (sublane repair-pins)"
    ),
    SIGNATURE_RELEASE_NOT_SETTLED: (
        "the lane's process release is not durably 'released' (never requested, still in "
        "flight, or stored non-canonically) — an actuator may be closing its slots right now"
    ),
    SIGNATURE_REPLACEMENT_IN_FLIGHT: (
        "a receiver replacement is in flight for this lane"
    ),
}


@dataclass(frozen=True)
class WorktreeBindingRepairOutcome:
    """The typed outcome the operator / automation reads."""

    state: str
    issue: str
    lane: str
    executed: bool = False
    revision: int = 0
    reason: str = ""
    detail: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.state == REPAIR_BLOCKED

    def as_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "issue": self.issue,
            "lane": self.lane,
            "executed": self.executed,
            "revision": self.revision,
            "reason": self.reason or None,
            "detail": self.detail,
            "is_blocked": self.is_blocked,
        }


def _norm(value: Optional[str]) -> str:
    return (value or "").strip()


def _current_branch(path: str) -> str:
    """The worktree's current branch, or ``""`` (fail-closed). Read-only."""
    if not path or not Path(path).is_dir():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, capture_output=True,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _repo_toplevel(path: str) -> str:
    """The worktree root ``path`` belongs to, or ``""`` (fail-closed). Read-only."""
    if not path or not Path(path).is_dir():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            text=True, capture_output=True,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def run_worktree_binding_repair(
    *,
    repo_root: Path,
    issue: str,
    lane: str,
    journal: str,
    worktree: str,
    execute: bool,
    lifecycle_home: Optional[Path] = None,
    workspace_id: Optional[str] = None,
) -> WorktreeBindingRepairOutcome:
    """Preflight (default) or perform the bounded metadata-only binding repair."""
    from mozyo_bridge.core.state.lane_lifecycle import (
        DISPOSITION_HIBERNATED,
        DecisionPointer,
        DecisionPointerError,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )
    from mozyo_bridge.core.state.lane_lifecycle_model import (
        BINDING_KIND_ISSUE,
        RELEASE_RELEASED,
        replacement_settled,
        validate_declared_slots,
    )
    from mozyo_bridge.core.state.lane_worktree_binding_repair import (
        LaneWorktreeBindingRepairStore,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        _norm_lane,
        derive_lane_workspace_token,
    )

    issue_id, lane_id, anchor = _norm(issue), _norm(lane), _norm(journal)

    def blocked(reason: str, detail: str) -> WorktreeBindingRepairOutcome:
        return WorktreeBindingRepairOutcome(
            state=REPAIR_BLOCKED, issue=issue_id, lane=lane_id,
            executed=execute, reason=reason, detail=detail,
        )

    if not (issue_id and lane_id and anchor and _norm(worktree)):
        return blocked(
            BLOCK_IDENTITY_INCOMPLETE,
            "a repair needs --issue, --lane, --journal and --worktree; zero write",
        )
    try:
        ws = _norm(workspace_id) or repo_scope_workspace_id(repo_root)
        key = LaneLifecycleKey(ws, lane_id)
        decision = DecisionPointer(source="redmine", issue_id=issue_id, journal_id=anchor)
    except (DecisionPointerError, ValueError, OSError):
        return blocked(
            BLOCK_IDENTITY_INCOMPLETE,
            "the workspace identity or the durable decision anchor is incomplete; zero write",
        )
    try:
        record = LaneLifecycleStore(home=lifecycle_home).get(key)
    except (LaneLifecycleError, ValueError, OSError):
        return blocked(BLOCK_STORE_UNREADABLE, "the lifecycle store is unreadable; zero write")
    if record is None:
        return blocked(BLOCK_ROW_ABSENT, "no lifecycle row owns this lane; zero write")
    # Review j#88526 F2: classify the row's signature through the SHARED pure classifier the
    # store's CAS uses. Re-deriving these axes here is exactly what let a normalizing preflight
    # report a green the raw CAS then refused — three rounds running. The preflight now cannot
    # reach a verdict the CAS would not.
    signature = classify_repair_signature(record, issue_id=issue_id)
    if signature != SIGNATURE_OK:
        return blocked(
            _SIGNATURE_BLOCKERS[signature],
            _SIGNATURE_DETAILS[signature] + "; zero write",
        )
    try:
        pins = tuple(record.declared_pins)
    except Exception:  # noqa: BLE001 - the classifier already proved these decode + validate
        pins = ()
    if not pins:
        return blocked(
            BLOCK_MISSING_PINS,
            "the row carries no decodable declared pins; repair its pins first "
            "(sublane repair-pins); zero write",
        )

    # Positive evidence that the named worktree IS this lane's — the row has no token to
    # compare against (that absence is the defect), so an asserted --worktree is never
    # trusted on its own.
    worktree_path = str(Path(_norm(worktree)).expanduser())
    # The RAW branch, checked before normalization: ``_norm_lane("")`` yields the "default"
    # lane, so normalizing first would read an unreadable checkout as the default lane — and
    # would let an unreadable worktree pass this fence outright on a lane named "default".
    raw_branch = _current_branch(worktree_path)
    if not raw_branch:
        return blocked(
            BLOCK_WORKTREE_UNREADABLE,
            "--worktree does not resolve to a live git checkout; zero write",
        )
    # Review j#88493: a branch NAME is not an identity. Any repository can hold a branch
    # called ``issue_…``, so "some git checkout whose branch matches" would let a FOREIGN
    # repo's token be written into this workspace's lane row. Two further axes close that:
    #   (a) the path must be the worktree ROOT itself, not a subdirectory of one (a subdir
    #       answers the same branch query but derives a different canonical token);
    #   (b) the worktree must resolve to THIS lane record's workspace through the canonical
    #       resolver — the one that makes a linked worktree inherit its main checkout's
    #       identity — so a same-named branch in another repository resolves elsewhere and is
    #       refused.
    top = _repo_toplevel(worktree_path)
    if not top:
        return blocked(
            BLOCK_WORKTREE_UNREADABLE,
            "--worktree does not resolve to a live git checkout; zero write",
        )
    try:
        resolved = Path(worktree_path).expanduser().resolve()
        is_root = resolved == Path(top).expanduser().resolve()
    except OSError:
        is_root = False
    if not is_root:
        return blocked(
            BLOCK_WORKTREE_NOT_ROOT,
            "--worktree must be the worktree ROOT, not a subdirectory of one; zero write",
        )
    try:
        worktree_ws = repo_scope_workspace_id(resolved)
    except Exception:  # noqa: BLE001 - an unresolvable workspace fails closed
        worktree_ws = ""
    if not worktree_ws or worktree_ws != ws:
        return blocked(
            BLOCK_FOREIGN_WORKSPACE,
            "--worktree belongs to a different workspace than the lane record; a matching "
            "branch name in another repository is not this lane; zero write",
        )
    if _norm_lane(raw_branch) != _norm_lane(lane_id):
        return blocked(
            BLOCK_BRANCH_DRIFTED,
            "--worktree is not on this lane's branch (the lane id IS its branch); zero write",
        )
    try:
        # Review j#88494: derive from the CANONICAL (symlink-resolved) root the evidence
        # checks just established — the same ``resolved`` the root / workspace gates used, and
        # what ``derive_lane_workspace_token``'s contract requires ("the caller must pass a
        # symlink-resolved path so mint-time and resolve-time agree"). Hashing the raw
        # ``--worktree`` string would let a symlink alias pass every gate and then record a
        # token that the live recovery probes — which resolve — could never re-derive, i.e.
        # a binding that reads as ``worktree_identity_mismatch`` forever.
        token = _norm(derive_lane_workspace_token(str(resolved)))
    except Exception:  # noqa: BLE001 - an underivable token fails closed
        token = ""
    if not token:
        return blocked(
            BLOCK_TOKEN_UNDERIVABLE,
            "the canonical worktree token could not be derived from --worktree; zero write",
        )

    # The already-bound fence is evaluated AFTER the token is derived so a re-run of the exact
    # same repair is the idempotent success the store already implements, and only a binding
    # naming a DIFFERENT worktree is refused.
    bound = _norm(record.worktree_identity)
    if bound == token:
        return WorktreeBindingRepairOutcome(
            state=REPAIR_APPLIED, issue=issue_id, lane=lane_id, executed=execute,
            revision=record.revision,
            detail=(
                "the lane already carries exactly this canonical worktree binding; "
                "idempotent no-op (zero write)"
            ),
        )
    if bound:
        return blocked(
            BLOCK_ALREADY_BOUND,
            "the lane is already bound to a DIFFERENT worktree; this surface fills a gap and "
            "never re-binds a lane; zero write",
        )

    if not execute:
        return WorktreeBindingRepairOutcome(
            state=REPAIR_PREFLIGHT, issue=issue_id, lane=lane_id,
            revision=record.revision,
            detail=(
                "preflight: the hibernated row matches the repair signature (pins present, "
                "worktree unbound) and --execute would record the canonical token"
            ),
        )

    try:
        outcome = LaneWorktreeBindingRepairStore(
            home=lifecycle_home
        ).repair_hibernated_worktree_binding(
            key,
            expected_revision=record.revision,
            expected_generation=record.lane_generation,
            issue_id=issue_id,
            worktree_identity=token,
            declared_slots=pins,
            decision=decision,
        )
    except (LaneLifecycleError, DecisionPointerError, ValueError, OSError) as exc:
        return blocked(
            BLOCK_CAS_REFUSED,
            f"the bounded repair CAS failed ({type(exc).__name__}); zero write",
        )
    if not outcome.applied:
        return blocked(
            BLOCK_CAS_REFUSED,
            f"the bounded repair CAS refused ({outcome.reason}); zero write",
        )
    return WorktreeBindingRepairOutcome(
        state=REPAIR_APPLIED, issue=issue_id, lane=lane_id, executed=True,
        revision=outcome.revision,
        detail=(
            "the hibernated row now carries its canonical worktree binding; declared pins, "
            "disposition and generation are unchanged and no process was touched"
        ),
    )


def format_worktree_binding_repair_text(outcome: WorktreeBindingRepairOutcome) -> str:
    lines = [
        f"sublane repair-worktree-binding: {outcome.state}",
        f"  lane: {outcome.lane} (issue {outcome.issue})",
        f"  executed: {outcome.executed}  revision: {outcome.revision}",
    ]
    if outcome.reason:
        lines.append(f"  reason: {outcome.reason}")
    if outcome.detail:
        lines.append(f"  detail: {outcome.detail}")
    return "\n".join(lines)


def cmd_sublane_repair_worktree_binding(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    outcome = run_worktree_binding_repair(
        repo_root=Path(repo).expanduser() if repo else Path.cwd(),
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        worktree=getattr(args, "worktree", "") or "",
        execute=bool(getattr(args, "execute", False)),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_worktree_binding_repair_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_repair_worktree_binding_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "repair-worktree-binding",
        help=(
            "record the canonical worktree binding of ONE hibernated, pinned lane whose "
            "worktree_identity is empty (metadata only; preflight default)"
        ),
        description=(
            "The bounded, metadata-only convergence for a hibernated / released lane whose "
            "declared pins are present but whose canonical worktree_identity is EMPTY — the "
            "shape that blocks sublane recover-pair / recover-gateway on "
            "lane_worktree_binding_unverified and that the active-only #13809 backfill cannot "
            "reach. Writes one lifecycle payload field (worktree_identity), plus the decision / "
            "revision audit metadata, under an exact revision + generation CAS, only "
            "after the named --worktree is positively shown to be this lane's (a live git "
            "checkout on the lane's own branch). No process is launched, closed, resumed, or "
            "sent to; the lane stays hibernated and recover-pair remains the surface that acts."
        ),
    )
    add_repo_option(parser)
    parser.add_argument("--issue", required=True, help="Redmine issue id the lane owns")
    parser.add_argument("--lane", required=True, help="exact lane id (also its branch)")
    parser.add_argument(
        "--journal", required=True,
        help="Redmine journal id of the durable decision authorizing this repair",
    )
    parser.add_argument(
        "--worktree", required=True,
        help="the lane's worktree path (verified to be a live checkout on the lane's branch)",
    )
    parser.add_argument(
        "--execute", action="store_true", help="write (default is a read-only preflight)"
    )
    parser.add_argument("--json", action="store_true", help="emit the structured outcome")
    parser.set_defaults(func=cmd_sublane_repair_worktree_binding)


__all__ = (
    "REPAIR_APPLIED",
    "REPAIR_PREFLIGHT",
    "REPAIR_BLOCKED",
    "BLOCK_IDENTITY_INCOMPLETE",
    "BLOCK_STORE_UNREADABLE",
    "BLOCK_ROW_ABSENT",
    "BLOCK_NOT_HIBERNATED",
    "BLOCK_WRONG_ISSUE",
    "BLOCK_ALREADY_BOUND",
    "BLOCK_MISSING_PINS",
    "BLOCK_PROJECT_SCOPE",
    "BLOCK_BINDING_KIND",
    "BLOCK_INVALID_PINS",
    "BLOCK_PINS_NOT_CANONICAL",
    "BLOCK_RELEASE_NOT_SETTLED",
    "BLOCK_REPLACEMENT_IN_FLIGHT",
    "BLOCK_WORKTREE_UNREADABLE",
    "BLOCK_WORKTREE_NOT_ROOT",
    "BLOCK_FOREIGN_WORKSPACE",
    "BLOCK_BRANCH_DRIFTED",
    "BLOCK_TOKEN_UNDERIVABLE",
    "BLOCK_CAS_REFUSED",
    "WorktreeBindingRepairOutcome",
    "run_worktree_binding_repair",
    "format_worktree_binding_repair_text",
    "cmd_sublane_repair_worktree_binding",
    "register_sublane_repair_worktree_binding_parser",
)
