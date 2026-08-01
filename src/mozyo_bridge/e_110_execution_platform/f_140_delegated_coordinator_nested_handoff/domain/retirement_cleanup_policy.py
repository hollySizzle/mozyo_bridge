"""Post-close lane retirement / cleanup state machine (Redmine #13686).

The **retirement half** of the #13686 actuator, deliberately a separate machine from
:mod:`...domain.auto_integration_policy` (design consultation answer j#77124, 必須訂正1).
Integration ends at ``integrated``; the issue then closes; and only after that does this
machine run — releasing the managed process, removing the worktree, and safe-deleting the
local branch. Folding the two together is what inverted the real order in the #12604
use case, so they never share a state and this one never merges, pushes, or integrates.

Each stage is its own recorded outcome (the acceptance's "段階別 outcome"): a step that ran,
a step that does not apply, and a step that was refused are three different facts, and the
durable record says which. Re-running is safe because every step is bound to the
integration action key that authorized it (:class:`CleanupActionRecord`), so a partial
failure resumes without deleting anything twice — and a *different* action key refuses
outright (:data:`~...domain.auto_integration_records.BLOCKED_ACTION_KEY_MISMATCH`) rather
than being silently ignored, because a destructive step must not inherit some other
action's authorization.

The safety rules are the ones j#77124 必須訂正2 fixed, and they are not config-reachable:

- ``git worktree remove`` runs only against a **clean** worktree at the **exact registered
  path**, and never with ``--force``. An unclean or unregistered path is refused, not forced.
- The local branch is **never** deleted with ``git branch -D``. A CAS-safe delete requires
  all of: no worktree still holds the branch; no unique unpushed commit; the branch is
  reachable from the target or is patch-equivalent to it; and the ref tip still equals the
  source head recorded in the action. The last is the compare-and-swap: a branch that moved
  since the action was formed is a different branch than the one that was integrated.
- There is **no remote-branch delete**. R1 shipped one behind a config toggle and review
  j#96344 finding 1 found it bypassed every local condition (disabling the local delete
  skipped the CAS checks and then deleted the remote ref anyway) and had no compare-and-swap
  against the remote tip. A real CAS on a remote ref needs ``--force-with-lease``, which is a
  force and therefore prohibited (j#96335) — so the operation cannot be made safe in this
  tranche and is not offered at all rather than offered unsafely. Whether a non-force CAS
  path exists is an owner/design question, not an implementation detail. Nothing here
  force-pushes, deletes, or rewrites any remote ref.
- A foreign worktree / ref, an unconfirmed integration, an unsettled CI, or an owed callback
  stops every subsequent destructive step at a safe interruption point.
- In a **non-Git** workspace the worktree / branch steps are explicit ``not_applicable`` and
  the process retire still runs on its own.

Pure: no IO and no discovery, mirroring the sibling policies (frozen inputs / outputs,
literal machine-readable vocabularies, ``as_payload`` dicts). The durable-record renderer
lives in :mod:`...domain.auto_integration_journal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    BLOCKED_ACTION_KEY_MISMATCH,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_NOT_APPLICABLE,
    OUTCOME_PENDING,
    StepOutcome,
    completed_steps,
)

# ---------------------------------------------------------------------------
# Steps, in the only order they may run.
# ---------------------------------------------------------------------------

#: Release the managed pane / process. Independent of Git: it is the one step a non-Git
#: lane still performs.
STEP_PROCESS_RETIRE = "process_retire"
#: ``git worktree remove`` — clean, exact registered path, never ``--force``.
STEP_WORKTREE_REMOVE = "worktree_remove"
#: The CAS-safe local branch delete (never ``git branch -D``). The ONLY ref-deleting step:
#: every ref this machine can delete is gated by the compare-and-swap conditions below, so
#: no toggle can leave a delete running with its conditions unevaluated (R1 review j#96344
#: finding 1).
STEP_LOCAL_BRANCH_DELETE = "local_branch_delete"

CLEANUP_STEPS: Tuple[str, ...] = (
    STEP_PROCESS_RETIRE,
    STEP_WORKTREE_REMOVE,
    STEP_LOCAL_BRANCH_DELETE,
)

#: The steps that delete a ref. Named so the invariant "every ref delete is CAS-gated" is
#: stateable — and testable — rather than implicit in the order of a function body.
REF_DELETING_STEPS: frozenset = frozenset({STEP_LOCAL_BRANCH_DELETE})

# ---------------------------------------------------------------------------
# States.
# ---------------------------------------------------------------------------

#: Gates are being evaluated; like its integration sibling this is the entry phase and
#: never a returned resting state.
STATE_CLEANUP_PREFLIGHT = "cleanup_preflight"
STATE_PROCESS_RETIRING = "process_retiring"
STATE_WORKTREE_REMOVING = "worktree_removing"
STATE_BRANCH_CLEANUP = "branch_cleanup"
#: Every applicable step reached a settled outcome.
STATE_RETIRED = "retired"
#: Fail-closed: no further destructive step runs.
STATE_CLEANUP_BLOCKED = "cleanup_blocked"

CLEANUP_STATES: frozenset = frozenset(
    {
        STATE_CLEANUP_PREFLIGHT,
        STATE_PROCESS_RETIRING,
        STATE_WORKTREE_REMOVING,
        STATE_BRANCH_CLEANUP,
        STATE_RETIRED,
        STATE_CLEANUP_BLOCKED,
    }
)

CLEANUP_TERMINAL_STATES: frozenset = frozenset({STATE_RETIRED, STATE_CLEANUP_BLOCKED})

# ---------------------------------------------------------------------------
# Blocked reasons.
# ---------------------------------------------------------------------------

#: The lane's issue is not closed. Cleanup is a post-close activity by construction.
BLOCKED_ISSUE_NOT_CLOSED = "issue_not_closed"
#: The integration is not confirmed (not ``integrated`` / ``already_integrated`` /
#: ``patch_equivalent``). Removing the checkout of unintegrated work discards it.
BLOCKED_INTEGRATION_UNCONFIRMED = "integration_unconfirmed"
#: CI on the integration head has not settled. "Not yet red" is not green.
BLOCKED_CI_UNSETTLED = "integration_ci_unsettled"
#: An owed coordinator callback is unresolved.
BLOCKED_UNRESOLVED_CALLBACK = "unresolved_callback"
#: An owner / release gate is unresolved.
BLOCKED_UNRESOLVED_OWNER_GATE = "unresolved_owner_gate"
#: The worktree or ref belongs to another lane. Refused outright.
BLOCKED_FOREIGN_WORKTREE = "foreign_worktree"
#: The worktree has uncommitted / untracked changes. Removing it would discard them, and
#: ``--force`` is not an available answer.
BLOCKED_DIRTY_WORKTREE = "dirty_worktree"
#: The path to remove is not the exact path registered for this lane.
BLOCKED_WORKTREE_PATH_UNREGISTERED = "worktree_path_unregistered"
#: A worktree still has the branch checked out, so deleting the ref would orphan it.
BLOCKED_BRANCH_CHECKED_OUT = "branch_still_checked_out"
#: The branch holds commits that exist nowhere else. Deleting it would lose them.
BLOCKED_UNPUSHED_COMMITS = "unpushed_unique_commits"
#: The branch is neither reachable from the target nor shown patch-equivalent to it.
BLOCKED_NOT_INTEGRATED_REF = "branch_not_reachable_from_target"
#: The branch tip is no longer the source head the action recorded — the compare-and-swap
#: failed, so this is not the branch that was integrated.
BLOCKED_BRANCH_TIP_DRIFT = "branch_tip_drift"

_BLOCKED_REASON_PRECEDENCE: Tuple[str, ...] = (
    BLOCKED_ACTION_KEY_MISMATCH,
    BLOCKED_FOREIGN_WORKTREE,
    BLOCKED_ISSUE_NOT_CLOSED,
    BLOCKED_INTEGRATION_UNCONFIRMED,
    BLOCKED_CI_UNSETTLED,
    BLOCKED_UNRESOLVED_OWNER_GATE,
    BLOCKED_UNRESOLVED_CALLBACK,
    BLOCKED_WORKTREE_PATH_UNREGISTERED,
    BLOCKED_DIRTY_WORKTREE,
    BLOCKED_BRANCH_CHECKED_OUT,
    BLOCKED_UNPUSHED_COMMITS,
    BLOCKED_NOT_INTEGRATED_REF,
    BLOCKED_BRANCH_TIP_DRIFT,
)


def _order_reasons(reasons: Iterable[str]) -> Tuple[str, ...]:
    """Order blocked reasons by precedence, appending unknown ones deterministically."""
    collected = {str(reason).strip() for reason in reasons if str(reason).strip()}
    known = tuple(r for r in _BLOCKED_REASON_PRECEDENCE if r in collected)
    return known + tuple(sorted(r for r in collected if r not in _BLOCKED_REASON_PRECEDENCE))


# ---------------------------------------------------------------------------
# Policy, action record, preflight.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetirementCleanupPolicy:
    """The resolved cleanup policy intent (domain mirror of the config block).

    Intent only: each flag can turn a step *off*, and none of them can turn a safety gate
    off — no gate below reads a policy field.

    There is deliberately no ``delete_remote_branch`` field. R1 had one, and review j#96344
    finding 1 showed the shape of the bug it enabled: turning the *local* delete off skipped
    that step's CAS conditions and then let the remote delete run regardless. The lesson is
    structural — a toggle must not be able to skip the evaluation of conditions a later step
    depends on — and the fix is that the only ref-deleting step left is the one whose
    conditions are its own (:data:`REF_DELETING_STEPS`).
    """

    remove_worktree: bool = True
    delete_local_branch: bool = True

    @classmethod
    def default(cls) -> "RetirementCleanupPolicy":
        return cls()


@dataclass(frozen=True)
class CleanupActionRecord:
    """What this cleanup acts on, and the integration action that authorized it.

    ``integration_action_key`` is the exact
    :attr:`~...domain.auto_integration_policy.IntegrationActionRecord.action_key` of the
    integration that put this lane's work on the target. Every step is recorded under it, so
    a resume is idempotent and an unrelated action cannot borrow this authorization.

    ``recorded_source_head`` is the branch tip that integration ran against; the local
    delete compares the live tip against it as a compare-and-swap.
    """

    issue: str
    lane_generation: int
    branch: str
    worktree_path: str
    recorded_source_head: str
    integration_action_key: str

    @property
    def action_key(self) -> str:
        """The cleanup idempotency key — the lane identity plus its authorizing action."""
        return "|".join(
            (
                f"issue={self.issue}",
                f"lane_generation={self.lane_generation}",
                f"branch={self.branch}",
                f"worktree_path={self.worktree_path}",
                f"recorded_source_head={self.recorded_source_head}",
                f"integration_action_key={self.integration_action_key}",
            )
        )


@dataclass(frozen=True)
class CleanupPreflight:
    """The action-time facts the cleanup decision is made from (supplied, not discovered).

    Every safety-bearing field defaults to its **unsatisfied** value, so a caller that omits
    one is blocked rather than default-admitted.

    Always enforced:

    - ``issue_closed`` — cleanup is post-close by construction.
    - ``integration_confirmed`` — the integration reached ``integrated`` /
      ``already_integrated`` / ``patch_equivalent``
      (:attr:`~...domain.auto_integration_policy.IntegrationDecision.integrated`).
    - ``integration_ci_settled_green`` — CI on the integration head settled green.
    - ``callbacks_drained`` / ``owner_gates_resolved``.
    - ``authorizing_action_key`` — the integration action key the caller is acting under.
      Compared for equality against the record's; a mismatch refuses.

    Git-shaped (consulted only when ``is_git_workspace``):

    - ``worktree_is_foreign`` — the checkout belongs to another lane.
    - ``worktree_clean`` — no uncommitted / untracked changes.
    - ``worktree_path_registered`` — the path is the exact registered one for this lane.
    - ``branch_checked_out_elsewhere`` — some worktree still holds the branch.
    - ``unpushed_unique_commits`` — the branch holds commits that exist nowhere else.
    - ``branch_reachable_from_target`` / ``branch_patch_equivalent`` — either satisfies the
      "the work survives the delete" condition; ``patch_equivalent`` requires explicit
      evidence exactly as in the integration machine.
    - ``branch_tip`` — the live ref tip, compared against the record's
      ``recorded_source_head``.
    """

    is_git_workspace: bool
    authorizing_action_key: str = ""
    # Always enforced.
    issue_closed: bool = False
    integration_confirmed: bool = False
    integration_ci_settled_green: bool = False
    callbacks_drained: bool = False
    owner_gates_resolved: bool = False
    # Git-shaped.
    worktree_is_foreign: bool = True
    worktree_clean: bool = False
    worktree_path_registered: bool = False
    branch_checked_out_elsewhere: bool = True
    unpushed_unique_commits: bool = True
    branch_reachable_from_target: bool = False
    branch_patch_equivalent: bool = False
    branch_tip: str = ""


# ---------------------------------------------------------------------------
# Decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupDecision:
    """The result of :func:`decide_cleanup`.

    ``next_step`` is the single step the actuator may perform now (``None`` in a terminal
    state). ``step_outcomes`` records what this decision determined about *every* step —
    ``done`` from the ledger, ``not_applicable`` for a step this workspace or policy does not
    have, ``blocked`` for a refused one, ``pending`` for one not yet reached — so the durable
    record is a complete stage table rather than a single verdict.
    """

    state: str
    action_key: str
    next_step: Optional[str] = None
    step_outcomes: Tuple[Tuple[str, str], ...] = ()
    blocked_reasons: Tuple[str, ...] = ()
    primary_reason: Optional[str] = None
    reason: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.state == STATE_CLEANUP_BLOCKED

    @property
    def is_terminal(self) -> bool:
        return self.state in CLEANUP_TERMINAL_STATES

    def outcome_for(self, step: str) -> str:
        """This decision's outcome for ``step`` (``pending`` when it says nothing)."""
        for name, outcome in self.step_outcomes:
            if name == step:
                return outcome
        return OUTCOME_PENDING

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "action_key": self.action_key,
            "next_step": self.next_step,
            "step_outcomes": {name: outcome for name, outcome in self.step_outcomes},
            "blocked_reasons": list(self.blocked_reasons),
            "primary_reason": self.primary_reason,
            "reason": self.reason,
        }


def _table(**outcomes: str) -> Tuple[Tuple[str, str], ...]:
    """A stage table in the fixed :data:`CLEANUP_STEPS` order (unnamed steps are pending)."""
    return tuple((step, outcomes.get(step, OUTCOME_PENDING)) for step in CLEANUP_STEPS)


def decide_cleanup(
    policy: RetirementCleanupPolicy,
    record: CleanupActionRecord,
    preflight: CleanupPreflight,
    *,
    ledger: Iterable[StepOutcome] = (),
) -> CleanupDecision:
    """Decide the next cleanup state / step for one lane (pure).

    Evaluation order:

    1. The authorizing action key must match the record's, or nothing runs.
    2. The always-enforced gates are collected in full; any failure is
       :data:`STATE_CLEANUP_BLOCKED` and **no** step runs — not even the non-destructive
       process retire, because these gates are what establish that the lane is finished.
    3. The process retire runs first, and is the only step a non-Git lane has.
    4. The worktree removal runs next, behind its own gates (clean, registered, not foreign).
    5. The local branch delete runs last, behind the CAS conditions. It is the only step
       that deletes a ref, so no toggle can leave a ref delete running with its conditions
       unevaluated (R1 review j#96344 finding 1).

    Steps a policy turned off, and steps a non-Git workspace does not have, are reported
    ``not_applicable`` rather than skipped silently.
    """
    action_key = record.action_key

    if preflight.authorizing_action_key != record.integration_action_key:
        return CleanupDecision(
            state=STATE_CLEANUP_BLOCKED,
            action_key=action_key,
            next_step=None,
            step_outcomes=_table(),
            blocked_reasons=(BLOCKED_ACTION_KEY_MISMATCH,),
            primary_reason=BLOCKED_ACTION_KEY_MISMATCH,
            reason=(
                "the authorization offered is for a different integration action; a "
                "destructive step never inherits another action's authorization"
            ),
        )

    blockers: set[str] = set()
    if not preflight.issue_closed:
        blockers.add(BLOCKED_ISSUE_NOT_CLOSED)
    if not preflight.integration_confirmed:
        blockers.add(BLOCKED_INTEGRATION_UNCONFIRMED)
    if not preflight.integration_ci_settled_green:
        blockers.add(BLOCKED_CI_UNSETTLED)
    if not preflight.callbacks_drained:
        blockers.add(BLOCKED_UNRESOLVED_CALLBACK)
    if not preflight.owner_gates_resolved:
        blockers.add(BLOCKED_UNRESOLVED_OWNER_GATE)
    if preflight.is_git_workspace and preflight.worktree_is_foreign:
        blockers.add(BLOCKED_FOREIGN_WORKTREE)
    if blockers:
        ordered = _order_reasons(blockers)
        return CleanupDecision(
            state=STATE_CLEANUP_BLOCKED,
            action_key=action_key,
            next_step=None,
            step_outcomes=_table(),
            blocked_reasons=ordered,
            primary_reason=ordered[0],
            reason="cleanup refused before any step; nothing removed and no ref deleted",
        )

    done = completed_steps(ledger, action_key=action_key)
    outcomes: dict[str, str] = {step: OUTCOME_DONE for step in done if step in CLEANUP_STEPS}

    # 1. Process retire — Git-independent.
    if STEP_PROCESS_RETIRE not in outcomes:
        return CleanupDecision(
            state=STATE_PROCESS_RETIRING,
            action_key=action_key,
            next_step=STEP_PROCESS_RETIRE,
            step_outcomes=_table(**outcomes),
            reason="releasing the managed process (independent of the Git steps)",
        )

    if not preflight.is_git_workspace:
        outcomes[STEP_WORKTREE_REMOVE] = OUTCOME_NOT_APPLICABLE
        outcomes[STEP_LOCAL_BRANCH_DELETE] = OUTCOME_NOT_APPLICABLE
        return CleanupDecision(
            state=STATE_RETIRED,
            action_key=action_key,
            next_step=None,
            step_outcomes=_table(**outcomes),
            reason=(
                "not a Git workspace; the process was released and the worktree / branch "
                "steps do not apply"
            ),
        )

    # 2. Worktree removal — clean, exact registered path, never --force.
    if not policy.remove_worktree:
        outcomes[STEP_WORKTREE_REMOVE] = OUTCOME_NOT_APPLICABLE
    elif STEP_WORKTREE_REMOVE not in outcomes:
        worktree_blockers: set[str] = set()
        if not preflight.worktree_path_registered:
            worktree_blockers.add(BLOCKED_WORKTREE_PATH_UNREGISTERED)
        if not preflight.worktree_clean:
            worktree_blockers.add(BLOCKED_DIRTY_WORKTREE)
        if worktree_blockers:
            ordered = _order_reasons(worktree_blockers)
            outcomes[STEP_WORKTREE_REMOVE] = OUTCOME_BLOCKED
            return CleanupDecision(
                state=STATE_CLEANUP_BLOCKED,
                action_key=action_key,
                next_step=None,
                step_outcomes=_table(**outcomes),
                blocked_reasons=ordered,
                primary_reason=ordered[0],
                reason=(
                    "worktree removal refused; `--force` is not an available answer and "
                    "the branch delete that would follow does not run"
                ),
            )
        return CleanupDecision(
            state=STATE_WORKTREE_REMOVING,
            action_key=action_key,
            next_step=STEP_WORKTREE_REMOVE,
            step_outcomes=_table(**outcomes),
            reason=(
                f"removing the clean, registered worktree at {record.worktree_path} "
                "without --force"
            ),
        )

    # 3. Local branch delete — CAS-safe, never `git branch -D`.
    if not policy.delete_local_branch:
        outcomes[STEP_LOCAL_BRANCH_DELETE] = OUTCOME_NOT_APPLICABLE
    elif STEP_LOCAL_BRANCH_DELETE not in outcomes:
        branch_blockers: set[str] = set()
        if preflight.branch_checked_out_elsewhere:
            branch_blockers.add(BLOCKED_BRANCH_CHECKED_OUT)
        if preflight.unpushed_unique_commits:
            branch_blockers.add(BLOCKED_UNPUSHED_COMMITS)
        if not (
            preflight.branch_reachable_from_target or preflight.branch_patch_equivalent
        ):
            branch_blockers.add(BLOCKED_NOT_INTEGRATED_REF)
        if preflight.branch_tip != record.recorded_source_head:
            branch_blockers.add(BLOCKED_BRANCH_TIP_DRIFT)
        if branch_blockers:
            ordered = _order_reasons(branch_blockers)
            outcomes[STEP_LOCAL_BRANCH_DELETE] = OUTCOME_BLOCKED
            return CleanupDecision(
                state=STATE_CLEANUP_BLOCKED,
                action_key=action_key,
                next_step=None,
                step_outcomes=_table(**outcomes),
                blocked_reasons=ordered,
                primary_reason=ordered[0],
                reason=(
                    "local branch delete refused; `git branch -D` is never the fallback "
                    "and no remote ref is touched"
                ),
            )
        return CleanupDecision(
            state=STATE_BRANCH_CLEANUP,
            action_key=action_key,
            next_step=STEP_LOCAL_BRANCH_DELETE,
            step_outcomes=_table(**outcomes),
            reason=(
                f"CAS-safe delete of local branch {record.branch} at its recorded tip "
                f"{record.recorded_source_head}"
            ),
        )

    # There is no step 4. The remote ref is never touched — see the module docstring.
    return CleanupDecision(
        state=STATE_RETIRED,
        action_key=action_key,
        next_step=None,
        step_outcomes=_table(**outcomes),
        reason="every applicable cleanup step reached a settled outcome",
    )


__all__ = (
    "STEP_PROCESS_RETIRE",
    "STEP_WORKTREE_REMOVE",
    "STEP_LOCAL_BRANCH_DELETE",
    "CLEANUP_STEPS",
    "REF_DELETING_STEPS",
    "STATE_CLEANUP_PREFLIGHT",
    "STATE_PROCESS_RETIRING",
    "STATE_WORKTREE_REMOVING",
    "STATE_BRANCH_CLEANUP",
    "STATE_RETIRED",
    "STATE_CLEANUP_BLOCKED",
    "CLEANUP_STATES",
    "CLEANUP_TERMINAL_STATES",
    "BLOCKED_ISSUE_NOT_CLOSED",
    "BLOCKED_INTEGRATION_UNCONFIRMED",
    "BLOCKED_CI_UNSETTLED",
    "BLOCKED_UNRESOLVED_CALLBACK",
    "BLOCKED_UNRESOLVED_OWNER_GATE",
    "BLOCKED_FOREIGN_WORKTREE",
    "BLOCKED_DIRTY_WORKTREE",
    "BLOCKED_WORKTREE_PATH_UNREGISTERED",
    "BLOCKED_BRANCH_CHECKED_OUT",
    "BLOCKED_UNPUSHED_COMMITS",
    "BLOCKED_NOT_INTEGRATED_REF",
    "BLOCKED_BRANCH_TIP_DRIFT",
    "RetirementCleanupPolicy",
    "CleanupActionRecord",
    "CleanupPreflight",
    "CleanupDecision",
    "decide_cleanup",
)
