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

The value objects the decision reasons over live in the sibling
:mod:`...domain.auto_integration_records` and are re-exported here for a stable public
surface. R1 review j#96344 is why they are objects at all: four of this module's inputs were
bare booleans, and a boolean cannot be audited. ``integration_ci_green`` claimed a green run
without naming which run, which check, or which commit; ``coordinator_confirmed`` claimed an
approval without naming who, of what, or where it is written; the integration worktree was a
string never checked against being the lane's own; and the configured integration branch was
declared and read by nothing. Each is now a record that carries the identity its claim
depends on, and each is validated against the exact action it is offered for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    BLOCKED_ACTION_KEY_MISMATCH,
    CI_CONCLUSION_SUCCESS,
    CONFIRMATION_ISSUER_COORDINATOR,
    EMPTY_TARGET_HEAD,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_NOT_APPLICABLE,
    OUTCOME_PENDING,
    STEP_OUTCOMES,
    CoordinatorConfirmation,
    IntegrationActionRecord,
    IntegrationCiEvidence,
    IntegrationWorktree,
    StepOutcome,
    build_integration_action_record,
    completed_steps,
    is_full_sha,
    normalized_branch,
)

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
# What the exact-SHA CI gate did (R1 review j#96344 finding 2 / dispute j#96346).
# ---------------------------------------------------------------------------

#: The gate was required and satisfied by checkable evidence for the exact pushed commit.
CI_GATE_GREEN = "green"
#: The gate was turned off by config. The integration completed WITHOUT an observed CI run,
#: and the record says so rather than letting `integrated` imply a green one.
CI_GATE_WAIVED = "waived"
#: The decision never got as far as the CI gate (blocked, terminal no-op, or still pushing).
CI_GATE_NOT_REACHED = "not_reached"

CI_GATE_STATES: frozenset = frozenset(
    {CI_GATE_GREEN, CI_GATE_WAIVED, CI_GATE_NOT_REACHED}
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
# Steps. The outcome vocabulary and the ledger live in the records sibling, so the two
# state machines share one spelling of "done" rather than each defining its own.
# ---------------------------------------------------------------------------

STEP_INTEGRATION_APPLY = "integration_apply"
STEP_PUSH = "push"
STEP_INTEGRATION_CI = "integration_ci"

INTEGRATION_STEPS: Tuple[str, ...] = (
    STEP_INTEGRATION_APPLY,
    STEP_PUSH,
    STEP_INTEGRATION_CI,
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
#: CI on the exact integration SHA settled with a non-success conclusion.
BLOCKED_INTEGRATION_CI_FAILED = "integration_ci_failed"
#: CI evidence was supplied but cannot be checked: a missing run id, a missing required-check
#: identity, a malformed head (R1 review j#96344 finding 2). Distinct from a red conclusion —
#: "we cannot tell what this run was" is not "the run failed", and the operator's next action
#: differs (produce a complete record vs fix the build).
BLOCKED_INTEGRATION_CI_EVIDENCE_INCOMPLETE = "integration_ci_evidence_incomplete"
#: The CI evidence is about a different commit than the one the push landed. A green run on
#: the previous integration, on a sibling branch, or on the source before the merge is not
#: this action's CI, and a bare boolean could not tell the difference.
BLOCKED_INTEGRATION_CI_HEAD_MISMATCH = "integration_ci_head_mismatch"
#: The action's target ref is not the integration branch this actuator is configured for
#: (R1 review j#96344 finding 4). Distinct from :data:`BLOCKED_UNKNOWN_TARGET`, which asks
#: whether the ref is a known integration branch at all: a ref can be perfectly well known
#: and still not be the one the operator pointed THIS actuator at.
BLOCKED_TARGET_NOT_CONFIGURED = "target_not_configured"
#: A coordinator confirmation was supplied but does not authorize this action: it names a
#: different action key, was not issued by the coordinator role, or carries no durable anchor
#: (R1 review j#96344 finding 5). Absence is not this — that is
#: :data:`STATE_CONFIRMATION_REQUIRED`; this is a confirmation that is present and invalid.
BLOCKED_CONFIRMATION_INADMISSIBLE = "coordinator_confirmation_inadmissible"
#: The dedicated integration worktree is unusable: unregistered, not clean, or — the one this
#: exists for — the lane's own checkout, which must never check out the target branch
#: (j#77124 / R1 review j#96344 finding 3).
BLOCKED_INTEGRATION_WORKTREE_INADMISSIBLE = "integration_worktree_inadmissible"

#: Precedence for the *primary* reason, most fundamental first. The full set is always
#: reported too, so a durable record shows every failing gate rather than only the first.
_BLOCKED_REASON_PRECEDENCE: Tuple[str, ...] = (
    BLOCKED_ACTION_RECORD_INVALID,
    BLOCKED_MODE_UNRECOGNIZED,
    BLOCKED_ACTION_KEY_MISMATCH,
    BLOCKED_CONFIRMATION_INADMISSIBLE,
    BLOCKED_FOREIGN_WORKTREE,
    BLOCKED_UNKNOWN_TARGET,
    BLOCKED_TARGET_NOT_CONFIGURED,
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
    BLOCKED_INTEGRATION_WORKTREE_INADMISSIBLE,
    BLOCKED_INTEGRATION_CI_EVIDENCE_INCOMPLETE,
    BLOCKED_INTEGRATION_CI_HEAD_MISMATCH,
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
      Note this is a *different* question from whether the ref is the branch this actuator
      was configured for; that one is checked against ``policy.integration_branch``.
    - ``source_ci_green`` — settled green, not merely started.
    - ``callbacks_drained`` / ``owner_gates_resolved``.

    Typed records (R1 review j#96344 — each replaced a bare boolean that could not be
    audited; all default to ``None``, which is the unsatisfied reading):

    - ``integration_ci`` — :class:`IntegrationCiEvidence`: the CI verdict for the exact
      commit the push landed, carrying the required check's identity and the run id so an
      unrelated green run cannot satisfy it. Read only when ``require_integration_ci``.
    - ``coordinator_confirmation`` — :class:`CoordinatorConfirmation`: an explicit
      confirmation of *this* action key by the coordinator role, with the durable anchor it
      is recorded at. Read only under :data:`MODE_COORDINATOR_CONFIRMED`. It gates actuation;
      it relaxes nothing.
    - ``integration_worktree`` — :class:`IntegrationWorktree`: the dedicated checkout a
      merge-commit disposition is applied in, with the measured facts proving it is not the
      lane's own. Read only when the effective disposition is a merge commit.
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
    callbacks_drained: bool = False
    owner_gates_resolved: bool = False
    # Typed records (R1 review j#96344). `None` is the unsatisfied reading throughout.
    integration_ci: Optional[IntegrationCiEvidence] = None
    coordinator_confirmation: Optional[CoordinatorConfirmation] = None
    integration_worktree: Optional[IntegrationWorktree] = None


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

    ``integration_ci`` says, machine-readably, what the exact-SHA CI gate did for this
    decision — :data:`CI_GATE_GREEN`, :data:`CI_GATE_WAIVED`, or :data:`CI_GATE_NOT_REACHED`.
    It exists because ``state == integrated`` alone cannot distinguish "CI was green on this
    exact commit" from "the operator turned the CI gate off", and a durable record that
    cannot distinguish them will be read as the first (R1 review j#96344 finding 2; the
    knob itself is owner-authorized — j#96335 lists branch/target CI as config-driven — so
    the waiver is made visible rather than removed. Dispute record: j#96346).
    """

    state: str
    action_key: str
    next_step: Optional[str] = None
    disposition: str = DISPOSITION_FAST_FORWARD
    blocked_reasons: Tuple[str, ...] = ()
    primary_reason: Optional[str] = None
    reason: str = ""
    integration_ci: str = CI_GATE_NOT_REACHED

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
            "integration_ci": self.integration_ci,
        }


def _blocked(
    record: IntegrationActionRecord,
    reasons: Iterable[str],
    *,
    disposition: str,
    action_key: str = "",
    reason: str = "",
) -> IntegrationDecision:
    ordered = _order_reasons(reasons)
    return IntegrationDecision(
        state=STATE_INTEGRATION_BLOCKED,
        action_key=action_key or record.action_key,
        next_step=None,
        disposition=disposition,
        blocked_reasons=ordered,
        primary_reason=ordered[0] if ordered else None,
        reason=reason or "integration refused; no side effect performed",
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
    # R1 review j#96344 finding 4: the CONFIGURED branch must actually constrain the push.
    # `target_identity_known` asks whether the ref is a known integration branch at all;
    # this asks whether it is the one the operator pointed THIS actuator at. Without it the
    # policy's `integration_branch` was declared and read by nothing, and a record naming a
    # different target integrated happily. `None` means runtime resolution, which is the
    # documented "no configured constraint" case, so it imposes none.
    if policy.integration_branch is not None and normalized_branch(
        policy.integration_branch
    ) != normalized_branch(record.target_ref):
        blockers.add(BLOCKED_TARGET_NOT_CONFIGURED)
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

    if policy.mode == MODE_COORDINATOR_CONFIRMED:
        # R1 review j#96344 finding 5: a confirmation must name WHAT it confirms, WHO issued
        # it, and WHERE it is recorded. A bare flag said none of those, so any caller could
        # assert one. Absence and inadmissibility are kept apart: the first is a normal wait,
        # the second is a refusal.
        confirmation = preflight.coordinator_confirmation
        if confirmation is None:
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
        problems = confirmation.admissibility_errors(action_key=action_key)
        if problems:
            return _blocked(
                record,
                (BLOCKED_CONFIRMATION_INADMISSIBLE,),
                disposition=disposition,
                action_key=action_key,
                reason="; ".join(problems),
            )

    done = completed_steps(ledger, action_key=action_key)

    # 1. Apply. A fast-forward has nothing to apply, so the step is skipped as
    #    `not_applicable` rather than reported done.
    if disposition == DISPOSITION_MERGE_COMMIT:
        if STEP_INTEGRATION_APPLY not in done:
            # R1 review j#96344 finding 3: j#77124 forbids the lane's worktree ever checking
            # out the target branch, and R1 asserted that in a docstring while enforcing
            # nothing — a caller passing the lane's own path made the actuator perform the
            # forbidden operation. The identity is now measured and checked before the step
            # is authorized, not after it is handed to git.
            worktree = preflight.integration_worktree
            problems = (
                ("no dedicated integration worktree was supplied",)
                if worktree is None
                else worktree.admissibility_errors()
            )
            if problems:
                return _blocked(
                    record,
                    (BLOCKED_INTEGRATION_WORKTREE_INADMISSIBLE,),
                    disposition=disposition,
                    action_key=action_key,
                    reason="; ".join(problems),
                )
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
    #
    # R1 review j#96344 finding 2: the gate used to read a bare `integration_ci_green: bool`,
    # which said a run was green without saying WHICH run, which required check, or which
    # commit — so an unrelated green run satisfied it. The evidence now names all three and
    # is matched against the head the push actually landed. The head comes from the ledger,
    # not from a caller-supplied field, so the two cannot drift apart.
    ci_gate = CI_GATE_NOT_REACHED
    if policy.require_integration_ci:
        landed_head = done[STEP_PUSH].head or record.source_head
        evidence = preflight.integration_ci
        if evidence is None:
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
        incomplete = evidence.completeness_errors()
        if incomplete:
            # "We cannot tell what this run was" is not "the run failed": the operator's next
            # action is to produce a complete record, not to fix a build.
            return _blocked(
                record,
                (BLOCKED_INTEGRATION_CI_EVIDENCE_INCOMPLETE,),
                disposition=disposition,
                action_key=action_key,
                reason="; ".join(incomplete),
            )
        if evidence.integration_head != landed_head:
            return _blocked(
                record,
                (BLOCKED_INTEGRATION_CI_HEAD_MISMATCH,),
                disposition=disposition,
                action_key=action_key,
                reason=(
                    f"the CI evidence is about {evidence.integration_head}, but the push "
                    f"landed {landed_head}"
                ),
            )
        if not evidence.is_green:
            return _blocked(
                record,
                (BLOCKED_INTEGRATION_CI_FAILED,),
                disposition=disposition,
                action_key=action_key,
                reason=(
                    f"required check {evidence.workflow!r} run {evidence.run!r} settled "
                    f"{evidence.conclusion!r} on {evidence.integration_head}"
                ),
            )
        ci_gate = CI_GATE_GREEN
    else:
        # The knob is owner-authorized (j#96335 lists branch/target CI as config-driven), so
        # the waiver is made VISIBLE rather than removed: `integrated` alone cannot say
        # whether CI ran, and a durable record that cannot say will be read as if it did.
        ci_gate = CI_GATE_WAIVED

    # The reason names only the gates that actually ran: with `require_integration_ci: false`
    # no exact-SHA CI was observed, and a durable record must not say one was.
    settled = (
        "origin reachability is settled and the exact-SHA CI gate is green"
        if ci_gate == CI_GATE_GREEN
        else "origin reachability is settled; the exact-SHA CI gate is WAIVED by config "
        "(no CI run was observed for this commit)"
    )
    return IntegrationDecision(
        state=STATE_INTEGRATED,
        action_key=action_key,
        next_step=None,
        disposition=disposition,
        reason=f"integrated into {record.target_ref}: {settled}",
        integration_ci=ci_gate,
    )


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
    "BLOCKED_INTEGRATION_CI_EVIDENCE_INCOMPLETE",
    "BLOCKED_INTEGRATION_CI_HEAD_MISMATCH",
    "BLOCKED_TARGET_NOT_CONFIGURED",
    "BLOCKED_CONFIRMATION_INADMISSIBLE",
    "BLOCKED_INTEGRATION_WORKTREE_INADMISSIBLE",
    "CI_GATE_GREEN",
    "CI_GATE_WAIVED",
    "CI_GATE_NOT_REACHED",
    "CI_GATE_STATES",
    "AutoIntegrationPolicy",
    "IntegrationPreflight",
    "IntegrationDecision",
    "decide_integration",
    # Re-exported from the records sibling for a stable public surface.
    "EMPTY_TARGET_HEAD",
    "is_full_sha",
    "normalized_branch",
    "OUTCOME_DONE",
    "OUTCOME_NOT_APPLICABLE",
    "OUTCOME_BLOCKED",
    "OUTCOME_PENDING",
    "STEP_OUTCOMES",
    "BLOCKED_ACTION_KEY_MISMATCH",
    "StepOutcome",
    "completed_steps",
    "IntegrationActionRecord",
    "build_integration_action_record",
    "CI_CONCLUSION_SUCCESS",
    "IntegrationCiEvidence",
    "CONFIRMATION_ISSUER_COORDINATOR",
    "CoordinatorConfirmation",
    "IntegrationWorktree",
)
