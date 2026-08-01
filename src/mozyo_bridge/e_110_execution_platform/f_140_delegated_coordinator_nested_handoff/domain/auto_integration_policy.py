"""Coordinator-owned gated auto-integration state machine (Redmine #13686).

This is the **integration half** of the actuator the owner authorized in #13686 j#96335:
the blanket "no auto-merge / no auto-integration mechanism" prohibition was too broad, so
what replaces it is not an ungated merge but a state machine whose every transition is a
durable gate. The implementer's direct push to an integration branch stays prohibited; a
*coordinator-owned* integration that has satisfied every gate may advance itself.

Deliberately separated from retirement (design consultation answer j#77124, 必須訂正1).
The pre-existing #12604 :mod:`...domain.sublane_integration_policy` requires
``issue_closed`` / callbacks drained / a durable retire record **before** its merge — wiring
a live merge into it would invert the real order, which is

    review approval -> integration -> exact-SHA CI -> task/US close -> retire

So integration runs here, ending at :data:`STATE_INTEGRATED`; only afterwards does the
close happen, and only after that does the separate post-close cleanup machine
(:mod:`...domain.retirement_cleanup_policy`) remove a worktree or delete a branch. The two
never share a state, and nothing here closes an issue, kills a pane, removes a worktree,
deletes any ref, or force-pushes.

The states (j#77124 必須訂正1, narrowed by the owner's ff-only default in j#96335):

1. :data:`STATE_INTEGRATION_PREFLIGHT` — revalidate *at action time*: the exact source head
   the review approved, origin reachability, the latest review generation, source CI, target
   identity / ref allowlist, the expected target head, a clean non-foreign source.
2. :data:`STATE_INTEGRATION_APPLY` — apply the recorded disposition in a **dedicated
   integration worktree**, never by checking the target branch out in the lane's worktree.
   With the ff-only default there is nothing to apply and this state is skipped.
3. :data:`STATE_PUSH_WAITING` — a normal, non-force push. Remote drift loses the race and
   fails closed; it is never resolved by a force or a rebase.
4. :data:`STATE_AWAITING_CI` — CI on the **exact integration SHA**, as an asynchronous gate.
   A single synchronous command never assumes CI completed.
5. :data:`STATE_INTEGRATED` — origin reachability plus exact-SHA CI green are both settled.

Idempotency (j#77124 必須訂正2). Every decision is bound to an
:class:`IntegrationActionRecord` whose :attr:`~IntegrationActionRecord.action_key` covers
``issue + lane_generation + source_head + target_ref + expected_target_head +
review_generation``. Steps already recorded ``done`` under that exact key are not re-run, so
a partial failure re-runs without duplicating a merge or a push; and any drift in those six
values yields a *different* key, so a stale ledger can never satisfy a new action.

``already_integrated`` and ``patch_equivalent`` are separate terminal dispositions, not
successes to be re-produced: the first is decided by target ancestry, the second only by
explicit patch-id evidence supplied by the caller. Neither performs a side effect.

Pure: no IO, no discovery. Every fact is supplied by the caller from the action-time probe
or the durable record, mirroring the :mod:`...domain.sublane_integration_policy` style
(frozen inputs / outputs, literal machine-readable vocabularies, ``as_payload`` dicts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

# ---------------------------------------------------------------------------
# Config-driven mode vocabulary (literal; machine-readable regardless of UI language).
# ---------------------------------------------------------------------------

#: Advance the integration automatically once every gate is satisfied.
MODE_AUTO = "auto"
#: Every gate is evaluated, but the actuation additionally waits for an explicit
#: coordinator confirmation of *this* action key. The gates are not relaxed by it — a
#: confirmation cannot un-block a blocked action.
MODE_COORDINATOR_CONFIRMED = "coordinator_confirmed"
#: No auto-integration at all: the decision is terminal and performs nothing. This is the
#: behavior-preserving default, so a repo that declares no ``auto_integration`` block keeps
#: today's fully manual coordinator integration.
MODE_DISABLED = "disabled"

INTEGRATION_MODES: frozenset = frozenset(
    {MODE_AUTO, MODE_COORDINATOR_CONFIRMED, MODE_DISABLED}
)

# ---------------------------------------------------------------------------
# Integration disposition vocabulary — HOW the source reaches the integration branch.
# ---------------------------------------------------------------------------

#: Fast-forward the integration branch to the source head. The owner's default (j#96335):
#: a normal non-force push of the source head to the target ref *is* a fast-forward, so no
#: new commit is created and there is nothing to conflict.
DISPOSITION_FAST_FORWARD = "fast_forward"
#: Create a merge commit in a dedicated integration worktree. Only reachable when the
#: operator turned ``ff_only`` off; a conflict fails closed and is never auto-resolved.
DISPOSITION_MERGE_COMMIT = "merge_commit"

INTEGRATION_DISPOSITIONS: frozenset = frozenset(
    {DISPOSITION_FAST_FORWARD, DISPOSITION_MERGE_COMMIT}
)

# ---------------------------------------------------------------------------
# States.
# ---------------------------------------------------------------------------

#: The machine's ENTRY state: the phase in which every gate below is evaluated. It is
#: deliberately never *returned* by :func:`decide_integration` — a preflight either finds a
#: failing gate (:data:`STATE_INTEGRATION_BLOCKED`) or hands off to a later state, so there
#: is no resting state in which an action is "in preflight". Named because j#77124 names it
#: as the first boundary of the machine, and pinned by test so a future change cannot quietly
#: turn it into a resting state that a consumer would read as progress.
STATE_INTEGRATION_PREFLIGHT = "integration_preflight"
STATE_INTEGRATION_APPLY = "integration_apply"
STATE_PUSH_WAITING = "push_waiting"
STATE_AWAITING_CI = "awaiting_ci"
STATE_INTEGRATED = "integrated"
#: Fail-closed. The lane is not integrated and nothing destructive follows.
STATE_INTEGRATION_BLOCKED = "integration_blocked"
#: Terminal, no side effect: the source head is already reachable from the target head.
STATE_ALREADY_INTEGRATED = "already_integrated"
#: Terminal, no side effect: the source is not an ancestor, but the caller supplied explicit
#: patch-id evidence that the same patches are already on the target. Kept apart from
#: :data:`STATE_ALREADY_INTEGRATED` so a durable record never claims ancestry it lacks.
STATE_PATCH_EQUIVALENT = "patch_equivalent"
#: Terminal: not a Git workspace, so there is no integration to perform. The separate
#: process retire still runs — see :mod:`...domain.retirement_cleanup_policy`.
STATE_NOT_APPLICABLE = "not_applicable"
#: Terminal for this evaluation: ``coordinator_confirmed`` mode with no confirmation for
#: this exact action key yet. Every gate already passed; only the confirmation is missing.
STATE_CONFIRMATION_REQUIRED = "coordinator_confirmation_required"
#: Terminal: ``mode: disabled``.
STATE_DISABLED = "disabled"

INTEGRATION_STATES: frozenset = frozenset(
    {
        STATE_INTEGRATION_PREFLIGHT,
        STATE_INTEGRATION_APPLY,
        STATE_PUSH_WAITING,
        STATE_AWAITING_CI,
        STATE_INTEGRATED,
        STATE_INTEGRATION_BLOCKED,
        STATE_ALREADY_INTEGRATED,
        STATE_PATCH_EQUIVALENT,
        STATE_NOT_APPLICABLE,
        STATE_CONFIRMATION_REQUIRED,
        STATE_DISABLED,
    }
)

#: The states from which no further step is ever produced.
TERMINAL_STATES: frozenset = frozenset(
    {
        STATE_INTEGRATED,
        STATE_INTEGRATION_BLOCKED,
        STATE_ALREADY_INTEGRATED,
        STATE_PATCH_EQUIVALENT,
        STATE_NOT_APPLICABLE,
        STATE_CONFIRMATION_REQUIRED,
        STATE_DISABLED,
    }
)

# ---------------------------------------------------------------------------
# Steps and their outcomes — the "段階別 outcome" the acceptance requires.
# ---------------------------------------------------------------------------

STEP_INTEGRATION_APPLY = "integration_apply"
STEP_PUSH = "push"
STEP_INTEGRATION_CI = "integration_ci"

INTEGRATION_STEPS: Tuple[str, ...] = (
    STEP_INTEGRATION_APPLY,
    STEP_PUSH,
    STEP_INTEGRATION_CI,
)

#: The step completed. A ``done`` outcome under the same action key is never re-run.
OUTCOME_DONE = "done"
#: The step does not apply to this action (a fast-forward disposition has nothing to apply;
#: a non-Git workspace has no integration at all). Distinct from ``done``: it asserts that
#: nothing happened, rather than that something succeeded.
OUTCOME_NOT_APPLICABLE = "not_applicable"
#: The step was refused. Recorded so a re-run explains itself rather than silently retrying.
OUTCOME_BLOCKED = "blocked"
#: The step was started and its result is not yet settled — the asynchronous CI gate's
#: normal reading. Never treated as success.
OUTCOME_PENDING = "pending"

STEP_OUTCOMES: frozenset = frozenset(
    {OUTCOME_DONE, OUTCOME_NOT_APPLICABLE, OUTCOME_BLOCKED, OUTCOME_PENDING}
)

# ---------------------------------------------------------------------------
# Blocked reasons. Every one of them is fail-closed: the action stops *before* the side
# effect, or at a safe interruption point, and nothing further in the pipeline runs.
# ---------------------------------------------------------------------------

#: The action record itself is not usable — a missing / malformed identity field. A record
#: that cannot name what it acts on cannot be acted on.
BLOCKED_ACTION_RECORD_INVALID = "action_record_invalid"
#: The configured mode is not in the closed vocabulary. Distinct from :data:`STATE_DISABLED`,
#: which says the operator turned the actuator off: an unrecognized mode says the declaration
#: is broken and someone must fix it. Reporting it as ``disabled`` would read as intentional
#: in a durable record and leave a misconfigured workspace looking correctly configured.
BLOCKED_MODE_UNRECOGNIZED = "auto_integration_mode_unrecognized"
#: A record offered as authorization for this action carries a *different* action key.
#: Six-field drift produces a new key precisely so a stale record cannot satisfy a new
#: action. Inside :func:`decide_integration` a mismatched LEDGER entry needs no reason — it
#: is simply not counted, which is the whole resume contract — so this token belongs to the
#: consumer that must refuse rather than ignore: the post-close cleanup machine
#: (:mod:`...domain.retirement_cleanup_policy`), whose destructive steps may only run under
#: the exact integration action that authorized them.
BLOCKED_ACTION_KEY_MISMATCH = "action_key_mismatch"
#: The source head is not the exact head the review approved — the source mutated after
#: review. Integrating it would ship unreviewed commits under a review's authority.
BLOCKED_SOURCE_MUTATED = "source_mutated_after_review"
#: The source head is not reachable from origin, so no auditor could replay it.
BLOCKED_SOURCE_UNREACHABLE = "source_head_unreachable"
#: The latest review generation is not admissible (no approval, a stale approval for an
#: older generation, or an unresolved blocking finding in the latest one).
BLOCKED_REVIEW_INADMISSIBLE = "review_generation_inadmissible"
#: Required source-branch CI is not green (or not settled).
BLOCKED_SOURCE_CI_NOT_GREEN = "source_ci_not_green"
#: The configured target ref is not a known, allowlisted integration branch.
BLOCKED_UNKNOWN_TARGET = "unknown_target_branch"
#: The observed target head differs from the expected one recorded in the action: the
#: target advanced since the action was formed. Resolved by re-forming the action against
#: the new head, never by forcing.
BLOCKED_TARGET_DRIFT = "target_drift"
#: The integration would not be a fast-forward and ``ff_only`` is in force.
BLOCKED_NON_FAST_FORWARD = "non_fast_forward"
#: The merge-commit disposition conflicted. Auto-resolution and auto-rebase are prohibited.
BLOCKED_MERGE_CONFLICT = "merge_conflict"
#: The source worktree is dirty; what would be integrated is not what was reviewed.
BLOCKED_DIRTY_WORKTREE = "dirty_worktree"
#: The worktree / branch is not this lane's — a destructive-adjacent op against someone
#: else's checkout is refused outright.
BLOCKED_FOREIGN_WORKTREE = "foreign_worktree"
#: The lane holds commits that exist nowhere on the remote.
BLOCKED_UNPUSHED_COMMITS = "unpushed_unique_commits"
#: An owed coordinator callback is unresolved.
BLOCKED_UNRESOLVED_CALLBACK = "unresolved_callback"
#: An owner decision / release gate is unresolved.
BLOCKED_UNRESOLVED_OWNER_GATE = "unresolved_owner_gate"
#: The push was attempted and lost the race (or otherwise failed). Recorded distinctly from
#: :data:`BLOCKED_TARGET_DRIFT`, which is the *pre*-push observation.
BLOCKED_PUSH_REJECTED = "push_rejected"
#: CI on the exact integration SHA failed.
BLOCKED_INTEGRATION_CI_FAILED = "integration_ci_failed"

#: Precedence for the *primary* reason, most fundamental first. The full set is always
#: reported too, so a durable record shows every failing gate rather than only the first.
_BLOCKED_REASON_PRECEDENCE: Tuple[str, ...] = (
    BLOCKED_ACTION_RECORD_INVALID,
    BLOCKED_MODE_UNRECOGNIZED,
    BLOCKED_ACTION_KEY_MISMATCH,
    BLOCKED_FOREIGN_WORKTREE,
    BLOCKED_UNKNOWN_TARGET,
    BLOCKED_REVIEW_INADMISSIBLE,
    BLOCKED_SOURCE_MUTATED,
    BLOCKED_SOURCE_UNREACHABLE,
    BLOCKED_UNPUSHED_COMMITS,
    BLOCKED_DIRTY_WORKTREE,
    BLOCKED_SOURCE_CI_NOT_GREEN,
    BLOCKED_UNRESOLVED_OWNER_GATE,
    BLOCKED_UNRESOLVED_CALLBACK,
    BLOCKED_TARGET_DRIFT,
    BLOCKED_NON_FAST_FORWARD,
    BLOCKED_MERGE_CONFLICT,
    BLOCKED_PUSH_REJECTED,
    BLOCKED_INTEGRATION_CI_FAILED,
)


def _order_reasons(reasons: Iterable[str]) -> Tuple[str, ...]:
    """Order blocked reasons by precedence, appending unknown ones deterministically.

    A reason a caller supplied that this table has not heard of must not vanish (the same
    silent-drop defect #14695 j#93807 finding 2 found in the sibling policy): unknown
    reasons are appended sorted, so the set is always complete and ``ordered[0]`` exists.
    """
    collected = {str(reason).strip() for reason in reasons if str(reason).strip()}
    known = tuple(r for r in _BLOCKED_REASON_PRECEDENCE if r in collected)
    return known + tuple(sorted(r for r in collected if r not in _BLOCKED_REASON_PRECEDENCE))


# ---------------------------------------------------------------------------
# Resolved policy (config intent, translated into the domain).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoIntegrationPolicy:
    """The resolved auto-integration policy intent (domain mirror of the config block).

    Intent only. Exactly as in the #12604 sibling, the policy may opt *out* of an action;
    it can never opt out of a safety gate, because no gate below reads a policy field.

    ``mode`` — :data:`MODE_AUTO` / :data:`MODE_COORDINATOR_CONFIRMED` / :data:`MODE_DISABLED`.
    ``integration_branch`` — the configured target ref; ``None`` defers to runtime
    resolution, and a runtime that cannot resolve one fails closed rather than guessing.
    ``ff_only`` — the owner's default (j#96335). ``False`` admits the merge-commit
    disposition; it never admits a rebase or a force push, which have no representation here.
    ``require_source_ci`` / ``require_integration_ci`` — whether the source-branch and
    exact-integration-SHA CI gates are required.
    """

    mode: str = MODE_DISABLED
    integration_branch: Optional[str] = None
    ff_only: bool = True
    require_source_ci: bool = True
    require_integration_ci: bool = True

    @classmethod
    def default(cls) -> "AutoIntegrationPolicy":
        return cls()

    @property
    def disposition(self) -> str:
        """The disposition this policy admits (never a rebase / force)."""
        return DISPOSITION_FAST_FORWARD if self.ff_only else DISPOSITION_MERGE_COMMIT


# ---------------------------------------------------------------------------
# The immutable action record and its idempotency key.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationActionRecord:
    """The immutable identity of one integration action (j#77124 必須訂正1 / 訂正2).

    Six fields, and :attr:`action_key` is exactly their tuple: ``issue``,
    ``lane_generation``, ``source_head``, ``target_ref``, ``expected_target_head``,
    ``review_generation``. Any drift in any of them yields a different key, which is what
    makes a re-run safe: a ledger recorded for the old key satisfies nothing under the new
    one, so a partial failure resumes and a changed world starts over.

    ``source_head`` and ``expected_target_head`` are exact full commit SHAs — a branch name
    is not a pin, and the whole point of the record is that the action is bound to the exact
    commits the gates were evaluated against.
    """

    issue: str
    lane_generation: int
    source_head: str
    target_ref: str
    expected_target_head: str
    review_generation: str

    @property
    def action_key(self) -> str:
        """The idempotency key: the six identity fields, in a fixed order."""
        return "|".join(
            (
                f"issue={self.issue}",
                f"lane_generation={self.lane_generation}",
                f"source_head={self.source_head}",
                f"target_ref={self.target_ref}",
                f"expected_target_head={self.expected_target_head}",
                f"review_generation={self.review_generation}",
            )
        )

    def validation_errors(self) -> Tuple[str, ...]:
        """The reasons this record cannot identify an action (empty iff usable).

        ``lane_generation`` must be a positive integer (``bool`` is rejected even though it
        is an ``int`` subclass, so ``lane_generation: true`` never reads as generation 1),
        and every string field must be non-empty. The two head fields must additionally be
        full 40-hex commit SHAs; ``expected_target_head`` may instead be the empty-target
        sentinel :data:`EMPTY_TARGET_HEAD` for a target ref that does not exist yet.
        """
        problems: list[str] = []
        if not str(self.issue).strip():
            problems.append("issue is empty")
        if isinstance(self.lane_generation, bool) or not isinstance(
            self.lane_generation, int
        ):
            problems.append("lane_generation must be an integer")
        elif self.lane_generation <= 0:
            problems.append("lane_generation must be positive")
        if not str(self.target_ref).strip():
            problems.append("target_ref is empty")
        if not str(self.review_generation).strip():
            problems.append("review_generation is empty")
        if not _is_full_sha(self.source_head):
            problems.append("source_head must be a full 40-hex commit SHA")
        if self.expected_target_head != EMPTY_TARGET_HEAD and not _is_full_sha(
            self.expected_target_head
        ):
            problems.append(
                "expected_target_head must be a full 40-hex commit SHA "
                f"(or {EMPTY_TARGET_HEAD!r} for a target that does not exist yet)"
            )
        return tuple(problems)


#: The ``expected_target_head`` sentinel for a target ref that does not exist yet. Spelled
#: explicitly rather than left as an empty string so "the target is empty" is a stated fact
#: and not an omitted field.
EMPTY_TARGET_HEAD = "none"

_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_full_sha(value: object) -> bool:
    """True iff ``value`` is exactly 40 lowercase hex digits (a full commit SHA)."""
    if not isinstance(value, str) or len(value) != 40:
        return False
    return all(character in _HEX_DIGITS for character in value)


# ---------------------------------------------------------------------------
# The step ledger — what has already happened under an action key.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepOutcome:
    """One recorded step outcome, bound to the action key it was performed under."""

    action_key: str
    step: str
    outcome: str
    detail: str = ""
    #: The exact commit the step produced, where it produces one (the integration head an
    #: apply created, or the head a push landed). Empty when the step produces no commit.
    head: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "action_key": self.action_key,
            "step": self.step,
            "outcome": self.outcome,
            "detail": self.detail,
            "head": self.head,
        }


def completed_steps(
    ledger: Iterable[StepOutcome], *, action_key: str
) -> dict[str, StepOutcome]:
    """The ``done`` steps recorded under exactly ``action_key`` (later wins).

    Entries under any other key are ignored rather than merged: that is the whole
    idempotency contract. Non-``done`` outcomes are also ignored — a ``blocked`` or
    ``pending`` step has not happened, so it must be evaluated again.
    """
    return {
        entry.step: entry
        for entry in ledger
        if entry.action_key == action_key and entry.outcome == OUTCOME_DONE
    }


# ---------------------------------------------------------------------------
# Action-time preflight facts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationPreflight:
    """The action-time facts the integration decision is made from (supplied, not discovered).

    Every safety-bearing field defaults to its **unsatisfied** value, so a caller that omits
    one is blocked rather than default-admitted. The two that describe the world rather than
    a gate (``already_integrated`` / ``patch_equivalent_evidence``) default to ``False``,
    which is also the conservative reading: "not known to be integrated" leads to the gates,
    never past them.

    Git-shaped facts (consulted only when ``is_git_workspace``):

    - ``observed_target_head`` — the target ref's head *right now*, compared against the
      action's ``expected_target_head``. :data:`EMPTY_TARGET_HEAD` for a ref that does not exist.
    - ``fast_forward_possible`` — the expected target head is an ancestor of the source head.
    - ``already_integrated`` — the source head is reachable from the target head.
    - ``patch_equivalent_evidence`` — explicit patch-id evidence that the same patches are
      already on the target. Only this admits :data:`STATE_PATCH_EQUIVALENT`; a bare "looks
      the same" never does.
    - ``merge_conflict`` — the merge-commit disposition conflicted.
    - ``source_worktree_dirty`` / ``worktree_is_foreign`` / ``unpushed_unique_commits``.

    Durable-record facts, always enforced:

    - ``source_head_matches_review`` — the source head is the exact head the review approved.
    - ``source_origin_reachable`` — that head is reachable from origin.
    - ``review_generation_admissible`` — the latest review generation is approved AND carries
      no unresolved blocking finding (never merely "an approval exists somewhere").
    - ``target_identity_known`` — the target ref is a known, allowlisted integration branch.
    - ``source_ci_green`` / ``integration_ci_green`` — settled green, not merely started.
    - ``callbacks_drained`` / ``owner_gates_resolved``.
    - ``coordinator_confirmed`` — an explicit confirmation of *this* action key, read only
      under :data:`MODE_COORDINATOR_CONFIRMED`. It gates actuation; it relaxes nothing.
    """

    is_git_workspace: bool
    # Git-shaped.
    observed_target_head: str = ""
    fast_forward_possible: bool = False
    already_integrated: bool = False
    patch_equivalent_evidence: bool = False
    merge_conflict: bool = False
    source_worktree_dirty: bool = True
    worktree_is_foreign: bool = True
    unpushed_unique_commits: bool = True
    # Durable-record.
    source_head_matches_review: bool = False
    source_origin_reachable: bool = False
    review_generation_admissible: bool = False
    target_identity_known: bool = False
    source_ci_green: bool = False
    integration_ci_green: bool = False
    callbacks_drained: bool = False
    owner_gates_resolved: bool = False
    coordinator_confirmed: bool = False


# ---------------------------------------------------------------------------
# Decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationDecision:
    """The result of :func:`decide_integration`.

    ``state`` is one of :data:`INTEGRATION_STATES`. ``next_step`` is the single step the
    actuator may perform now (``None`` in every terminal state and whenever a confirmation
    is outstanding) — one step at a time, so each is recorded before the next is decided.
    ``blocked_reasons`` is the full failing-gate set with ``primary_reason`` its most
    fundamental member; both are empty / ``None`` unless the state is
    :data:`STATE_INTEGRATION_BLOCKED`.
    """

    state: str
    action_key: str
    next_step: Optional[str] = None
    disposition: str = DISPOSITION_FAST_FORWARD
    blocked_reasons: Tuple[str, ...] = ()
    primary_reason: Optional[str] = None
    reason: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.state == STATE_INTEGRATION_BLOCKED

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def integrated(self) -> bool:
        """True for every disposition under which the source IS on the target.

        ``already_integrated`` and ``patch_equivalent`` count: they are the reasons no
        integration was performed, not failures. The post-close cleanup machine reads this
        as its integration-confirmed precondition.
        """
        return self.state in (
            STATE_INTEGRATED,
            STATE_ALREADY_INTEGRATED,
            STATE_PATCH_EQUIVALENT,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "action_key": self.action_key,
            "next_step": self.next_step,
            "disposition": self.disposition,
            "blocked_reasons": list(self.blocked_reasons),
            "primary_reason": self.primary_reason,
            "reason": self.reason,
        }


def _blocked(
    record: IntegrationActionRecord,
    reasons: Iterable[str],
    *,
    disposition: str,
    action_key: str = "",
) -> IntegrationDecision:
    ordered = _order_reasons(reasons)
    return IntegrationDecision(
        state=STATE_INTEGRATION_BLOCKED,
        action_key=action_key or record.action_key,
        next_step=None,
        disposition=disposition,
        blocked_reasons=ordered,
        primary_reason=ordered[0] if ordered else None,
        reason="integration refused; no side effect performed",
    )


def decide_integration(
    policy: AutoIntegrationPolicy,
    record: IntegrationActionRecord,
    preflight: IntegrationPreflight,
    *,
    ledger: Iterable[StepOutcome] = (),
) -> IntegrationDecision:
    """Decide the next integration state / step for one action (pure).

    Evaluation order, chosen so nothing destructive-adjacent is ever reached on a bad
    premise:

    1. The record must identify an action at all, and ``mode: disabled`` stops here.
    2. A non-Git workspace has no integration: :data:`STATE_NOT_APPLICABLE`.
    3. Every gate is collected — all of them, so the durable record shows the full set
       rather than the first failure. Any failing gate is :data:`STATE_INTEGRATION_BLOCKED`.
    4. Only on a fully clean gate set do the terminal no-op dispositions apply
       (``already_integrated`` by ancestry, then ``patch_equivalent`` by explicit evidence).
       They are checked *after* the gates so a blocked action is never reported as a
       success, and *before* any step so the same merge is never re-produced.
    5. The disposition is resolved: a fast-forward when possible; otherwise ``ff_only``
       blocks and a merge commit is applied. A conflict blocks.
    6. The ledger decides how far this action already got, and the next unfinished step is
       returned — one step per call.

    The reported ``disposition`` is the EFFECTIVE one, not the policy's preference: a
    fast-forwardable integration reports :data:`DISPOSITION_FAST_FORWARD` even under
    ``ff_only: false``, because no merge commit is created in that case and a durable record
    must not claim one. Until the preflight has been read, the policy's preference is the
    best available answer, so the early refusals report that.
    """
    disposition = policy.disposition

    invalid = record.validation_errors()
    if invalid:
        return IntegrationDecision(
            state=STATE_INTEGRATION_BLOCKED,
            action_key="",
            next_step=None,
            disposition=disposition,
            blocked_reasons=(BLOCKED_ACTION_RECORD_INVALID,),
            primary_reason=BLOCKED_ACTION_RECORD_INVALID,
            reason="; ".join(invalid),
        )

    action_key = record.action_key

    if policy.mode == MODE_DISABLED:
        return IntegrationDecision(
            state=STATE_DISABLED,
            action_key=action_key,
            next_step=None,
            disposition=disposition,
            reason="auto-integration disabled by config (mode: disabled)",
        )
    if policy.mode not in INTEGRATION_MODES:
        # An unrecognized mode is not a licence to act — but it is also not `disabled`. Falling
        # back to `auto` would integrate without intent; reporting `disabled` would make a
        # broken declaration read as a deliberate opt-out, so it is a typed refusal instead.
        # (The config loader rejects such a value outright; this path is reachable from a
        # programmatically constructed policy.)
        return IntegrationDecision(
            state=STATE_INTEGRATION_BLOCKED,
            action_key=action_key,
            next_step=None,
            disposition=disposition,
            blocked_reasons=(BLOCKED_MODE_UNRECOGNIZED,),
            primary_reason=BLOCKED_MODE_UNRECOGNIZED,
            reason=f"unrecognized auto-integration mode {policy.mode!r}; refusing to act",
        )

    if not preflight.is_git_workspace:
        return IntegrationDecision(
            state=STATE_NOT_APPLICABLE,
            action_key=action_key,
            next_step=None,
            disposition=disposition,
            reason=(
                "not a Git workspace; integration and branch cleanup do not apply "
                "(the separate process retire is unaffected)"
            ),
        )

    blockers: set[str] = set()
    if preflight.worktree_is_foreign:
        blockers.add(BLOCKED_FOREIGN_WORKTREE)
    if not preflight.target_identity_known:
        blockers.add(BLOCKED_UNKNOWN_TARGET)
    if not preflight.review_generation_admissible:
        blockers.add(BLOCKED_REVIEW_INADMISSIBLE)
    if not preflight.source_head_matches_review:
        blockers.add(BLOCKED_SOURCE_MUTATED)
    if not preflight.source_origin_reachable:
        blockers.add(BLOCKED_SOURCE_UNREACHABLE)
    if preflight.unpushed_unique_commits:
        blockers.add(BLOCKED_UNPUSHED_COMMITS)
    if preflight.source_worktree_dirty:
        blockers.add(BLOCKED_DIRTY_WORKTREE)
    if policy.require_source_ci and not preflight.source_ci_green:
        blockers.add(BLOCKED_SOURCE_CI_NOT_GREEN)
    if not preflight.owner_gates_resolved:
        blockers.add(BLOCKED_UNRESOLVED_OWNER_GATE)
    if not preflight.callbacks_drained:
        blockers.add(BLOCKED_UNRESOLVED_CALLBACK)
    if preflight.observed_target_head != record.expected_target_head:
        blockers.add(BLOCKED_TARGET_DRIFT)
    if blockers:
        return _blocked(record, blockers, disposition=disposition, action_key=action_key)

    # Terminal no-op dispositions. Checked only once every gate is clean, so a blocked
    # action is never reported as already integrated, and checked before any step so the
    # same merge is never re-produced (j#77124 必須訂正2).
    if preflight.already_integrated:
        return IntegrationDecision(
            state=STATE_ALREADY_INTEGRATED,
            action_key=action_key,
            next_step=None,
            disposition=disposition,
            reason=(
                f"source head {record.source_head} is already reachable from "
                f"{record.target_ref}; no merge and no push performed"
            ),
        )
    if preflight.patch_equivalent_evidence:
        return IntegrationDecision(
            state=STATE_PATCH_EQUIVALENT,
            action_key=action_key,
            next_step=None,
            disposition=disposition,
            reason=(
                "explicit patch-id evidence shows the same patches are already on "
                f"{record.target_ref}; no merge and no push performed"
            ),
        )

    # The EFFECTIVE disposition: what will actually happen, not what the policy prefers.
    # A fast-forwardable integration creates no merge commit even under `ff_only: false`.
    if preflight.fast_forward_possible:
        disposition = DISPOSITION_FAST_FORWARD
    else:
        if policy.ff_only:
            return _blocked(
                record,
                (BLOCKED_NON_FAST_FORWARD,),
                disposition=disposition,
                action_key=action_key,
            )
        disposition = DISPOSITION_MERGE_COMMIT
        if preflight.merge_conflict:
            return _blocked(
                record,
                (BLOCKED_MERGE_CONFLICT,),
                disposition=disposition,
                action_key=action_key,
            )

    if policy.mode == MODE_COORDINATOR_CONFIRMED and not preflight.coordinator_confirmed:
        return IntegrationDecision(
            state=STATE_CONFIRMATION_REQUIRED,
            action_key=action_key,
            next_step=None,
            disposition=disposition,
            reason=(
                "every gate passed; awaiting the coordinator's explicit confirmation of "
                f"action key {action_key}"
            ),
        )

    done = completed_steps(ledger, action_key=action_key)

    # 1. Apply. A fast-forward has nothing to apply, so the step is skipped as
    #    `not_applicable` rather than reported done.
    if disposition == DISPOSITION_MERGE_COMMIT:
        if STEP_INTEGRATION_APPLY not in done:
            return IntegrationDecision(
                state=STATE_INTEGRATION_APPLY,
                action_key=action_key,
                next_step=STEP_INTEGRATION_APPLY,
                disposition=disposition,
                reason=(
                    "applying the recorded merge disposition in a dedicated integration "
                    "worktree (the lane's worktree never checks out the target branch)"
                ),
            )

    # 2. Push — a normal, non-force push.
    if STEP_PUSH not in done:
        return IntegrationDecision(
            state=STATE_PUSH_WAITING,
            action_key=action_key,
            next_step=STEP_PUSH,
            disposition=disposition,
            reason=(
                f"pushing to {record.target_ref} with a normal non-force push; remote "
                "drift fails closed and is never resolved by a force or a rebase"
            ),
        )

    # 3. CI on the exact integration SHA — asynchronous, never assumed complete.
    if policy.require_integration_ci:
        if not preflight.integration_ci_green:
            if STEP_INTEGRATION_CI in done:
                # The step was recorded done, yet the exact-SHA CI is not green: the run
                # settled red. Reported as its own reason rather than as a still-pending gate.
                return _blocked(
                    record,
                    (BLOCKED_INTEGRATION_CI_FAILED,),
                    disposition=disposition,
                    action_key=action_key,
                )
            return IntegrationDecision(
                state=STATE_AWAITING_CI,
                action_key=action_key,
                next_step=STEP_INTEGRATION_CI,
                disposition=disposition,
                reason=(
                    "awaiting CI on the exact integration SHA; a single synchronous "
                    "command never assumes the run completed"
                ),
            )

    # The reason names only the gates that actually ran: with `require_integration_ci: false`
    # no exact-SHA CI was observed, and a durable record must not say one was.
    settled = (
        "origin reachability and exact-SHA CI are both settled"
        if policy.require_integration_ci
        else "origin reachability is settled; the exact-SHA CI gate is disabled by config"
    )
    return IntegrationDecision(
        state=STATE_INTEGRATED,
        action_key=action_key,
        next_step=None,
        disposition=disposition,
        reason=f"integrated into {record.target_ref}: {settled}",
    )


# ---------------------------------------------------------------------------
# Durable-record renderer.
# ---------------------------------------------------------------------------


def render_integration_action_journal(
    decision: IntegrationDecision,
    record: IntegrationActionRecord,
    *,
    integration_head: str = "",
) -> str:
    """Render an integration decision as a durable record (pure).

    Emits only machine-readable decision fields plus the action identity — never a private
    path or a pane id. ``integration_head`` is the exact commit the integration produced on
    the target, which for a merge commit differs from the source head; the two are kept
    separate for the same reason the Hibernate Evidence Marker Contract keeps them separate
    (a single head cannot prove a patch-equivalent or merge-commit integration).

    The heading is deliberately **not** a ``## Gate: <token>`` one. This is the actuator's
    decision record — an input to the coordinator's integration journal, not that journal's
    gate heading — and the central preset's ``### Gate Heading Canonical Literal`` reserves
    the ``## Gate:`` form for tokens the Gate Schema / Journal Templates define. Writing
    ``## Gate: integration_disposition`` would mint a gate token no vocabulary defines, which
    is exactly what the #14665 regression guard exists to catch. For the same reason this
    renderer does not emit the ``integration_disposition`` evidence marker: that marker is the
    coordinator's to write, from the canonical producer, on the coordinator's own journal.
    """
    # Keyed on ``is_blocked``, NOT on ``integrated``: an in-progress decision (push_waiting,
    # awaiting_ci, confirmation required) is neither, and rendering it under
    # ``## integration_blocked`` would put a refusal that never happened into a durable record.
    lines = [
        "## integration_blocked" if decision.is_blocked else "## integration action decision",
        "",
        f"- issue: #{record.issue}",
        f"- state: {decision.state}",
        f"- action_key: {decision.action_key}",
        f"- source_head: {record.source_head}",
        f"- integration_branch: {record.target_ref}",
        f"- expected_target_head: {record.expected_target_head}",
        f"- integration_head: {integration_head or 'none'}",
        f"- disposition: {decision.disposition}",
        f"- review_generation: {record.review_generation}",
    ]
    if decision.is_blocked:
        lines.append(f"- primary_reason: {decision.primary_reason}")
        lines.append("- blocked_reasons: " + ", ".join(decision.blocked_reasons))
        lines.append(
            "- next_action: coordinator callback (fail-closed; nothing integrated, "
            "no force push, no rebase, no ref deleted)"
        )
    else:
        lines.append(f"- next_step: {decision.next_step or 'none'}")
        lines.append(f"- reason: {decision.reason}")
    return "\n".join(lines)


__all__ = (
    "MODE_AUTO",
    "MODE_COORDINATOR_CONFIRMED",
    "MODE_DISABLED",
    "INTEGRATION_MODES",
    "DISPOSITION_FAST_FORWARD",
    "DISPOSITION_MERGE_COMMIT",
    "INTEGRATION_DISPOSITIONS",
    "STATE_INTEGRATION_PREFLIGHT",
    "STATE_INTEGRATION_APPLY",
    "STATE_PUSH_WAITING",
    "STATE_AWAITING_CI",
    "STATE_INTEGRATED",
    "STATE_INTEGRATION_BLOCKED",
    "STATE_ALREADY_INTEGRATED",
    "STATE_PATCH_EQUIVALENT",
    "STATE_NOT_APPLICABLE",
    "STATE_CONFIRMATION_REQUIRED",
    "STATE_DISABLED",
    "INTEGRATION_STATES",
    "TERMINAL_STATES",
    "STEP_INTEGRATION_APPLY",
    "STEP_PUSH",
    "STEP_INTEGRATION_CI",
    "INTEGRATION_STEPS",
    "OUTCOME_DONE",
    "OUTCOME_NOT_APPLICABLE",
    "OUTCOME_BLOCKED",
    "OUTCOME_PENDING",
    "STEP_OUTCOMES",
    "BLOCKED_ACTION_RECORD_INVALID",
    "BLOCKED_ACTION_KEY_MISMATCH",
    "BLOCKED_MODE_UNRECOGNIZED",
    "BLOCKED_SOURCE_MUTATED",
    "BLOCKED_SOURCE_UNREACHABLE",
    "BLOCKED_REVIEW_INADMISSIBLE",
    "BLOCKED_SOURCE_CI_NOT_GREEN",
    "BLOCKED_UNKNOWN_TARGET",
    "BLOCKED_TARGET_DRIFT",
    "BLOCKED_NON_FAST_FORWARD",
    "BLOCKED_MERGE_CONFLICT",
    "BLOCKED_DIRTY_WORKTREE",
    "BLOCKED_FOREIGN_WORKTREE",
    "BLOCKED_UNPUSHED_COMMITS",
    "BLOCKED_UNRESOLVED_CALLBACK",
    "BLOCKED_UNRESOLVED_OWNER_GATE",
    "BLOCKED_PUSH_REJECTED",
    "BLOCKED_INTEGRATION_CI_FAILED",
    "EMPTY_TARGET_HEAD",
    "AutoIntegrationPolicy",
    "IntegrationActionRecord",
    "StepOutcome",
    "completed_steps",
    "IntegrationPreflight",
    "IntegrationDecision",
    "decide_integration",
    "render_integration_action_journal",
)
