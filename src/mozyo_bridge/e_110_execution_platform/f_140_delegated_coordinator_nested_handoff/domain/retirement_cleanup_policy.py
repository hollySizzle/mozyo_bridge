"""Post-close lane retirement / cleanup state machine (Redmine #13686).

The **retirement half** of the #13686 actuator, deliberately a separate machine from
:mod:`...domain.auto_integration_policy` (design consultation answer j#77124, 必須訂正1).
Integration ends at ``integrated``; the issue then closes; and only after that does this
machine run — releasing the managed process. Folding the two together is what inverted the
real order in the #12604 use case, so they never share a state and this one never merges,
pushes, or integrates.

Each stage is its own recorded outcome (the acceptance's "段階別 outcome"): a step that ran,
a step that does not apply, and a step that was refused are three different facts, and the
durable record says which. Re-running is safe because every step is bound to the
integration action key that authorized it (:class:`CleanupActionRecord`), so a partial
failure resumes without deleting anything twice — and a *different* action key refuses
outright (:data:`~...domain.auto_integration_records.BLOCKED_ACTION_KEY_MISMATCH`) rather
than being silently ignored, because a destructive step must not inherit some other
action's authorization.

**This machine performs no Git operation at all.** Three were shipped and all three were
withdrawn across R7-R9, and the reason is one rule rather than three coincidences:

    an operation whose safety condition cannot be enforced by the operation itself
    is not offered.

- The **remote branch delete** (R1, retired by review j#96344 finding 1) bypassed every local
  condition and had no compare-and-swap against the remote tip; a real CAS on a remote ref
  needs ``--force-with-lease``, a force, prohibited by j#96335.
- The **local branch delete** (retired by review j#96396 finding 1) needed two conditions at
  once — the tip is still the recorded one, and no worktree holds the branch — and no git
  primitive enforces both in one invocation (measured on git 2.50.1): ``update-ref -d`` CASes
  the tip but deletes a branch a worktree is standing on; ``branch -D`` refuses the held
  branch atomically but takes no tip constraint; ``update-ref --stdin`` rejects ``verify`` +
  ``delete`` on one ref. The two-invocation form was reproduced destroying a commit that
  landed in the window, while the step recorded ``done``.
- The **worktree removal** (retired by review j#96401 finding 1) measured the checkout's lane
  identity with one command and removed it by *path* with another. Reproduced: swapping a
  foreign lane's clean checkout onto that path between the two removed the foreign checkout
  and recorded ``done``. ``git worktree remove`` takes no expected-identity argument, the
  admin entry name is reused after such a swap so it is not instance identity, and while
  ``git worktree lock`` does pin the path→entry binding against every git-level takeover,
  **no mutation can run while it is held** — ``remove`` and ``move`` both refuse a locked
  worktree and want ``-f -f`` — so the unlock that must precede the removal reopens the
  window. All measured; see the real-git tests.

What survives is the one step whose primitive takes the identity as its argument rather than
resolving it from a mutable name: ``release_process(issue, lane_generation)``. That is the
distinction, and it is worth stating plainly — a path and a ref name are late-bound, and
anything that binds them earlier than the mutation is a check, not a guarantee.

Removing the lane's worktree and branch remains an **operator** step in the
``preflight_sublane_retire`` runbook, where ``git worktree remove`` and ``git branch -d``
run with a human deciding.

The gates are unchanged and none of them is config-reachable: an unclosed issue, an
unconfirmed integration, an unsettled CI, an owed callback, an unresolved owner gate, or a
record that does not name this actuator's own lane stops the run at a safe interruption
point.

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

#: Release the managed pane / process — the only step, and the only one whose primitive is
#: parameterized by the identity it acts on (``issue`` + ``lane_generation``) rather than by
#: a late-bound name another actor can re-point. Independent of Git, so a non-Git lane
#: performs it too.
STEP_PROCESS_RETIRE = "process_retire"

CLEANUP_STEPS: Tuple[str, ...] = (STEP_PROCESS_RETIRE,)

#: The steps that touch the Git object / ref / worktree graph — **empty**, and kept as a
#: value rather than a sentence so the invariant is checkable rather than merely stated. The
#: three that once lived here (remote branch delete, local branch delete, worktree removal)
#: were each withdrawn for the same reason; see the module docstring. A future step belongs
#: here only once its primitive enforces its own conditions in a single operation.
GIT_MUTATING_STEPS: frozenset = frozenset()
#: For this machine "deletes a ref" and "touches Git at all" are now the same empty set; the
#: older name is kept so the invariant reads the same from either side.
REF_DELETING_STEPS: frozenset = GIT_MUTATING_STEPS

# ---------------------------------------------------------------------------
# States.
# ---------------------------------------------------------------------------

#: Gates are being evaluated; like its integration sibling this is the entry phase and
#: never a returned resting state.
STATE_CLEANUP_PREFLIGHT = "cleanup_preflight"
STATE_PROCESS_RETIRING = "process_retiring"
#: Every applicable step reached a settled outcome.
STATE_RETIRED = "retired"
#: Fail-closed: no further destructive step runs.
STATE_CLEANUP_BLOCKED = "cleanup_blocked"

CLEANUP_STATES: frozenset = frozenset(
    {
        STATE_CLEANUP_PREFLIGHT,
        STATE_PROCESS_RETIRING,
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
#: ``patch_equivalent``). Retiring a lane whose work is not on the target abandons it.
BLOCKED_INTEGRATION_UNCONFIRMED = "integration_unconfirmed"
#: CI on the integration head has not settled. "Not yet red" is not green.
BLOCKED_CI_UNSETTLED = "integration_ci_unsettled"
#: An owed coordinator callback is unresolved.
BLOCKED_UNRESOLVED_CALLBACK = "unresolved_callback"
#: An owner / release gate is unresolved.
BLOCKED_UNRESOLVED_OWNER_GATE = "unresolved_owner_gate"
#: The record does not name this actuator's own lane — issue, lane generation, branch and
#: worktree path must ALL match its configuration. Refused outright: releasing another lane's
#: managed process is a cross-lane side effect exactly as removing its checkout was.
#:
#: R9 kept the older ``foreign_worktree`` literal "for existing durable records"; review
#: j#96406 finding 4 pointed out that no such records exist — the production durable reader
#: and ledger are #14825, unimplemented — so the name was being kept wrong for a
#: compatibility that has no instances. It says what it means now.
BLOCKED_LANE_IDENTITY_MISMATCH = "lane_identity_mismatch"
#: The ledger's recorded steps are out of dependency order or carry foreign provenance.
BLOCKED_LEDGER_UNTRUSTWORTHY = "ledger_untrustworthy"

_BLOCKED_REASON_PRECEDENCE: Tuple[str, ...] = (
    BLOCKED_ACTION_KEY_MISMATCH,
    BLOCKED_LEDGER_UNTRUSTWORTHY,
    BLOCKED_LANE_IDENTITY_MISMATCH,
    BLOCKED_ISSUE_NOT_CLOSED,
    BLOCKED_INTEGRATION_UNCONFIRMED,
    BLOCKED_CI_UNSETTLED,
    BLOCKED_UNRESOLVED_OWNER_GATE,
    BLOCKED_UNRESOLVED_CALLBACK,
)


def _order_reasons(reasons: Iterable[str]) -> Tuple[str, ...]:
    """Order blocked reasons by precedence, appending unknown ones deterministically."""
    collected = {str(reason).strip() for reason in reasons if str(reason).strip()}
    known = tuple(r for r in _BLOCKED_REASON_PRECEDENCE if r in collected)
    return known + tuple(sorted(r for r in collected if r not in _BLOCKED_REASON_PRECEDENCE))


# ---------------------------------------------------------------------------
# Action record, preflight.
# ---------------------------------------------------------------------------

# There is deliberately no cleanup policy record and no cleanup config block. Every version
# of it was a set of toggles over Git steps (``delete_remote_branch``,
# ``delete_local_branch``, ``remove_worktree``), and review j#96344 finding 1 showed the bug
# that shape enables: turning one step off skipped the conditions a later step depended on.
# With every Git step withdrawn (:data:`GIT_MUTATING_STEPS`) the remaining step is
# unconditional, so a policy argument here could only be a field nothing reads — and the
# rule this issue keeps re-learning is that such a field is worse than absent, because
# setting it looks like buying something.


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
    against it, because no step reads a ref (:data:`GIT_MUTATING_STEPS`).

    ``branch`` and ``worktree_path`` are likewise identity, not targets: nothing in this
    machine removes a checkout or touches a ref. They name the lane so the actuator can
    refuse a record that is not its own.
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

    All of them are enforced; there is no longer a conditional group:

    - ``issue_closed`` — cleanup is post-close by construction.
    - ``integration_confirmed`` — the integration reached ``integrated`` /
      ``already_integrated`` / ``patch_equivalent``
      (:attr:`~...domain.auto_integration_policy.IntegrationDecision.integrated`).
    - ``integration_ci_settled_green`` — CI on the integration head settled green.
    - ``callbacks_drained`` / ``owner_gates_resolved``.
    - ``authorizing_action_key`` — the integration action key the caller is acting under.
      Compared for equality against the record's; a mismatch refuses.
    - ``lane_is_foreign`` — the record does not name the actuator's own lane. Answered by
      comparing the record against the actuator's own configuration, not by probing the
      filesystem: with no Git step left there is nothing on disk this decision acts on, and
      what still matters is that we do not retire another lane's managed process.

    **No Git-shaped field remains.** R7 carried five branch-shaped ones for the local branch
    delete and R8 three worktree-shaped ones for the removal; both steps were withdrawn
    (module docstring) and their fields went with them. The rule, learned three times on this
    issue: a field no gate reads is worse than absent, because supplying it looks like buying
    protection. ``is_git_workspace`` is gone for the same reason — every remaining step runs
    identically in a non-Git workspace.
    """

    authorizing_action_key: str = ""
    issue_closed: bool = False
    integration_confirmed: bool = False
    integration_ci_settled_green: bool = False
    callbacks_drained: bool = False
    owner_gates_resolved: bool = False
    lane_is_foreign: bool = True


# ---------------------------------------------------------------------------
# Decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupDecision:
    """The result of :func:`decide_cleanup`.

    ``next_step`` is the single step the actuator may perform now (``None`` in a terminal
    state). ``step_outcomes`` records what this decision determined about *every* step —
    ``done`` from the ledger, ``blocked`` for a refused one, ``pending`` for one not yet
    reached — so the durable record is a complete stage table rather than a single verdict.
    ``not_applicable`` is still part of the vocabulary and is still rendered, but no step
    reaches it any more: the steps that could be turned off or skipped were the Git ones.
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
    record: CleanupActionRecord,
    preflight: CleanupPreflight,
    *,
    ledger: Iterable[StepOutcome] = (),
    trusted_recorder: str = "",
) -> CleanupDecision:
    """Decide the next cleanup state / step for one lane (pure).

    There is no ``policy`` parameter. It carried the Git-step toggles, and with every Git
    step withdrawn there is nothing left to turn off — an argument that can no longer change
    an outcome is removed rather than accepted and ignored.

    Evaluation order:

    1. The authorizing action key must match the record's, or nothing runs.
    2. The gates are collected in full; any failure is :data:`STATE_CLEANUP_BLOCKED` and the
       step does not run — not even though it is non-destructive, because these gates are
       what establish that the lane is finished and that it is ours.
    3. The process retire runs. There is no step after it (:data:`CLEANUP_STEPS`).
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
    if preflight.lane_is_foreign:
        blockers.add(BLOCKED_LANE_IDENTITY_MISMATCH)
    if blockers:
        ordered = _order_reasons(blockers)
        return CleanupDecision(
            state=STATE_CLEANUP_BLOCKED,
            action_key=action_key,
            next_step=None,
            step_outcomes=_table(),
            blocked_reasons=ordered,
            primary_reason=ordered[0],
            reason=(
                "cleanup refused before any step; no process released (and this machine "
                "removes no checkout and deletes no ref at all)"
            ),
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

    # The one step. Its primitive takes the lane identity as its argument, which is the
    # property the three withdrawn Git steps could not offer (module docstring).
    if STEP_PROCESS_RETIRE not in outcomes:
        return CleanupDecision(
            state=STATE_PROCESS_RETIRING,
            action_key=action_key,
            next_step=STEP_PROCESS_RETIRE,
            step_outcomes=_table(**outcomes),
            reason=(
                f"releasing the managed process for issue {record.issue} generation "
                f"{record.lane_generation} (no checkout removed, no ref touched)"
            ),
        )

    # There is no step 2. This machine removes no checkout and deletes no ref — see the
    # module docstring for what each withdrawn step could not enforce about itself.
    return CleanupDecision(
        state=STATE_RETIRED,
        action_key=action_key,
        next_step=None,
        step_outcomes=_table(**outcomes),
        reason=(
            f"every applicable cleanup step reached a settled outcome; the worktree at "
            f"{record.worktree_path} and the local branch {record.branch} are left for the "
            "operator runbook"
        ),
    )


__all__ = (
    "STEP_PROCESS_RETIRE",
    "CLEANUP_STEPS",
    "GIT_MUTATING_STEPS",
    "REF_DELETING_STEPS",
    "STATE_CLEANUP_PREFLIGHT",
    "STATE_PROCESS_RETIRING",
    "STATE_RETIRED",
    "STATE_CLEANUP_BLOCKED",
    "CLEANUP_STATES",
    "CLEANUP_TERMINAL_STATES",
    "BLOCKED_ISSUE_NOT_CLOSED",
    "BLOCKED_INTEGRATION_UNCONFIRMED",
    "BLOCKED_CI_UNSETTLED",
    "BLOCKED_UNRESOLVED_CALLBACK",
    "BLOCKED_UNRESOLVED_OWNER_GATE",
    "BLOCKED_LANE_IDENTITY_MISMATCH",
    "BLOCKED_LEDGER_UNTRUSTWORTHY",
    "CleanupActionRecord",
    "CleanupPreflight",
    "CleanupDecision",
    "decide_cleanup",
)
