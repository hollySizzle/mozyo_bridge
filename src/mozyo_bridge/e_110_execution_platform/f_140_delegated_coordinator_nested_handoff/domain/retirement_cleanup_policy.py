"""Post-close lane retirement / cleanup state machine (Redmine #13686).

The **retirement half** of the #13686 actuator, deliberately a separate machine from
:mod:`...domain.auto_integration_policy` (design consultation answer j#77124, 必須訂正1).
Integration ends at ``integrated``; the issue then closes; and only after that does this
machine run — releasing the managed process and removing the worktree. Folding the two
together is what inverted the real order in the #12604 use case, so they never share a
state and this one never merges, pushes, or integrates.

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
- **This machine deletes no ref at all** — not a remote one and not a local one
  (:data:`REF_DELETING_STEPS` is empty, and that is asserted rather than described). Both
  deletes were shipped and both were removed for the same reason, which is worth stating
  once because it is the rule and not two coincidences: an operation whose safety condition
  cannot be enforced by the operation itself is not offered.

  R1's remote delete bypassed every local condition (disabling the local delete skipped the
  CAS checks and then deleted the remote ref anyway) and had no compare-and-swap against the
  remote tip; a real CAS on a remote ref needs ``--force-with-lease``, a force, prohibited by
  j#96335 (review j#96344 finding 1).

  The local delete needed two conditions at once — the ref tip still equals the recorded
  source head, and no worktree holds the branch — and **no git primitive enforces both in
  one invocation** (measured on git 2.50.1): ``update-ref -d <ref> <tip>`` compare-and-swaps
  the tip but deletes a branch a worktree is standing on, leaving that worktree's ``HEAD``
  unresolvable; ``branch -D`` refuses the held branch atomically but accepts no tip
  constraint; ``update-ref --stdin`` rejects ``verify`` + ``delete`` on one ref outright
  (``multiple updates for ref ... not allowed``). R7 split it into a verification followed by
  a delete, and review j#96396 finding 1 reproduced the window: a commit that landed between
  the two invocations was deleted with the branch and left reachable from no ref, while the
  step recorded ``done``. Deleting the lane branch remains an **operator** step in the
  ``preflight_sublane_retire`` runbook (``git branch -d``, which refuses unmerged work).
- A foreign worktree / ref, an unconfirmed integration, an unsettled CI, or an owed callback
  stops every subsequent destructive step at a safe interruption point.
- In a **non-Git** workspace the worktree step is an explicit ``not_applicable`` and the
  process retire still runs on its own.

Pure: no IO and no discovery, mirroring the sibling policies (frozen inputs / outputs,
literal machine-readable vocabularies, ``as_payload`` dicts). The durable-record renderer
lives in :mod:`...domain.auto_integration_journal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    BLOCKED_ACTION_KEY_MISMATCH,
    ledger_integrity_errors,
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
#: ``git worktree remove`` — clean, exact registered path, never ``--force``. The last step:
#: removing a checkout is recoverable from the ref, which is why it is the destructive
#: operation this machine kept.
STEP_WORKTREE_REMOVE = "worktree_remove"

CLEANUP_STEPS: Tuple[str, ...] = (
    STEP_PROCESS_RETIRE,
    STEP_WORKTREE_REMOVE,
)

#: The steps that delete a ref — **empty**, and kept as a value rather than a sentence so
#: the invariant is checkable. Both ref deletes this machine once had were removed (module
#: docstring); a future step that deletes a ref belongs here only once the delete enforces
#: its own conditions in a single operation.
REF_DELETING_STEPS: frozenset = frozenset()

# ---------------------------------------------------------------------------
# States.
# ---------------------------------------------------------------------------

#: Gates are being evaluated; like its integration sibling this is the entry phase and
#: never a returned resting state.
STATE_CLEANUP_PREFLIGHT = "cleanup_preflight"
STATE_PROCESS_RETIRING = "process_retiring"
STATE_WORKTREE_REMOVING = "worktree_removing"
#: Every applicable step reached a settled outcome.
STATE_RETIRED = "retired"
#: Fail-closed: no further destructive step runs.
STATE_CLEANUP_BLOCKED = "cleanup_blocked"

CLEANUP_STATES: frozenset = frozenset(
    {
        STATE_CLEANUP_PREFLIGHT,
        STATE_PROCESS_RETIRING,
        STATE_WORKTREE_REMOVING,
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
#: The ledger's recorded steps are out of dependency order or carry foreign provenance.
BLOCKED_LEDGER_UNTRUSTWORTHY = "ledger_untrustworthy"

_BLOCKED_REASON_PRECEDENCE: Tuple[str, ...] = (
    BLOCKED_ACTION_KEY_MISMATCH,
    BLOCKED_LEDGER_UNTRUSTWORTHY,
    BLOCKED_FOREIGN_WORKTREE,
    BLOCKED_ISSUE_NOT_CLOSED,
    BLOCKED_INTEGRATION_UNCONFIRMED,
    BLOCKED_CI_UNSETTLED,
    BLOCKED_UNRESOLVED_OWNER_GATE,
    BLOCKED_UNRESOLVED_CALLBACK,
    BLOCKED_WORKTREE_PATH_UNREGISTERED,
    BLOCKED_DIRTY_WORKTREE,
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

    There is deliberately no ``delete_remote_branch`` and no ``delete_local_branch`` field.
    R1 had both, and review j#96344 finding 1 showed the shape of the bug a delete toggle
    enabled: turning the *local* delete off skipped that step's CAS conditions and then let
    the remote delete run regardless. The lesson is structural — a toggle must not be able to
    skip the evaluation of conditions a later step depends on — and with no ref-deleting step
    left (:data:`REF_DELETING_STEPS`) there is no toggle that can do it.
    """

    remove_worktree: bool = True

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

    ``recorded_source_head`` is the branch tip that integration ran against. It is part of
    the identity this cleanup is bound to — a cleanup authorized for one source head is not
    the same action as one authorized for another — and nothing here compares a live tip
    against it, because no step reads a ref (:data:`REF_DELETING_STEPS`).
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

    There are deliberately no branch-shaped fields left. R7 carried five of them
    (``branch_checked_out_elsewhere``, ``unpushed_unique_commits``,
    ``branch_reachable_from_target``, ``branch_patch_equivalent``, ``branch_tip``) as the
    conditions of a local branch delete that review j#96396 finding 1 retired; a field no
    gate reads is worse than absent, because supplying it looks like buying protection.
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
    trusted_recorder: str = "",
) -> CleanupDecision:
    """Decide the next cleanup state / step for one lane (pure).

    Evaluation order:

    1. The authorizing action key must match the record's, or nothing runs.
    2. The always-enforced gates are collected in full; any failure is
       :data:`STATE_CLEANUP_BLOCKED` and **no** step runs — not even the non-destructive
       process retire, because these gates are what establish that the lane is finished.
    3. The process retire runs first, and is the only step a non-Git lane has.
    4. The worktree removal runs last, behind its own gates (clean, registered, not foreign).
       There is no step after it: no ref is deleted here (:data:`REF_DELETING_STEPS`).

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

    # R3 review j#96368 finding 3: provenance and order are checked before any destructive
    # step is selected. A ledger whose steps are out of order proves nothing about what ran.
    ledger_entries = tuple(ledger)
    integrity = ledger_integrity_errors(
        ledger_entries,
        action_key=action_key,
        required_order=CLEANUP_STEPS,
        recorded_by=trusted_recorder,
        known_steps=CLEANUP_STEPS,
    )
    if integrity:
        ordered = _order_reasons((BLOCKED_LEDGER_UNTRUSTWORTHY,))
        return CleanupDecision(
            state=STATE_CLEANUP_BLOCKED,
            action_key=action_key,
            next_step=None,
            step_outcomes=_table(),
            blocked_reasons=ordered,
            primary_reason=ordered[0],
            reason="; ".join(integrity),
        )
    done = completed_steps(
        ledger_entries, action_key=action_key, recorded_by=trusted_recorder
    )
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
        return CleanupDecision(
            state=STATE_RETIRED,
            action_key=action_key,
            next_step=None,
            step_outcomes=_table(**outcomes),
            reason=(
                "not a Git workspace; the process was released and the worktree step does "
                "not apply"
            ),
        )

    # 2. Worktree removal — clean, exact registered path, never --force. The last step.
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
                    "nothing else runs"
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

    # There is no step 3. No ref is deleted here, local or remote — see the module docstring
    # for what each delete could not enforce about itself.
    return CleanupDecision(
        state=STATE_RETIRED,
        action_key=action_key,
        next_step=None,
        step_outcomes=_table(**outcomes),
        reason=(
            f"every applicable cleanup step reached a settled outcome; local branch "
            f"{record.branch} is left for the operator runbook"
        ),
    )


__all__ = (
    "STEP_PROCESS_RETIRE",
    "STEP_WORKTREE_REMOVE",
    "CLEANUP_STEPS",
    "REF_DELETING_STEPS",
    "STATE_CLEANUP_PREFLIGHT",
    "STATE_PROCESS_RETIRING",
    "STATE_WORKTREE_REMOVING",
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
    "BLOCKED_LEDGER_UNTRUSTWORTHY",
    "RetirementCleanupPolicy",
    "CleanupActionRecord",
    "CleanupPreflight",
    "CleanupDecision",
    "decide_cleanup",
)
