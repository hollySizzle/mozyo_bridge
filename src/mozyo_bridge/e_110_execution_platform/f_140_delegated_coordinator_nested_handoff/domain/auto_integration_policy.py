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

So integration runs here, ending at :data:`STATE_INTEGRATED`; only afterwards does the close
happen, and only after that does the separate post-close cleanup machine
(:mod:`...domain.retirement_cleanup_policy`) release the lane's managed process. That machine
no longer removes a worktree or deletes a branch — all three of its destructive steps were
withdrawn (see its module docstring) — and nothing here closes an issue, kills a pane, removes
a worktree, deletes any ref, or force-pushes either.

The states (j#77124 必須訂正1, narrowed by the owner's ff-only default in j#96335):

1. :data:`STATE_INTEGRATION_PREFLIGHT` — revalidate *at action time*: the exact source head
   the review approved, origin reachability, the latest review generation, source CI, target
   identity / ref allowlist, the expected target head, a clean non-foreign source.
2. :data:`STATE_INTEGRATION_APPLY` — build the recorded disposition **as objects**
   (``merge-tree --write-tree`` + ``commit-tree``), touching no checkout, index or ref. With
   the ff-only default there is nothing to apply and this state is skipped.
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
approval without naming who, of what, or where it is written; and the configured integration
branch was declared and read by nothing. Each is now a record that carries the identity its claim
depends on, and each is validated against the exact action it is offered for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    BLOCKED_ACTION_KEY_MISMATCH,
    CI_CONCLUSION_SUCCESS,
    EMPTY_TARGET_HEAD,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_NOT_APPLICABLE,
    OUTCOME_PENDING,
    STEP_OUTCOMES,
    IntegrationActionRecord,
    IntegrationCiEvidence,
    IntegrationPreflight,
    LaneWorktree,
    checked_merge_status,
    StepOutcome,
    build_integration_action_record,
    completed_steps,
    is_full_sha,
    LEDGER_MISSING_HEAD,
    ledger_integrity_errors,
    normalized_branch,
)

# ---------------------------------------------------------------------------
# Config-driven mode vocabulary (literal; machine-readable regardless of UI language).
# ---------------------------------------------------------------------------

#: Advance the integration automatically once every gate is satisfied.
MODE_AUTO = "auto"
# There is deliberately no `coordinator_confirmed` mode. R3 shipped one whose confirmation
# was resolved through a port with no production binding, so the mode was not live-executable
# and any injected resolver could return a syntactically valid record for a fictitious anchor
# (R3 review j#96368 finding 4). The reviewer's two options were to wire a live resolver or to
# stop offering the mode until one exists; the second is taken here, because R4 already moves
# the whole preflight's measurement authority and adding a credentialed Redmine resolver in the
# same round would ship it under-verified. The design (a resolver that fresh-reads the anchor,
# matches the exact action key, and derives the issuer role from the record's author) is kept
# in `vibes/docs/logics/auto-integration-actuator.md` as the contract the follow-up implements.
#: No auto-integration at all: the decision is terminal and performs nothing. This is the
#: behavior-preserving default, so a repo that declares no ``auto_integration`` block keeps
#: today's fully manual coordinator integration.
MODE_DISABLED = "disabled"

INTEGRATION_MODES: frozenset = frozenset({MODE_AUTO, MODE_DISABLED})

# ---------------------------------------------------------------------------
# Integration disposition vocabulary — HOW the source reaches the integration branch.
# ---------------------------------------------------------------------------

#: Fast-forward the integration branch to the source head. The owner's default (j#96335):
#: a normal non-force push of the source head to the target ref *is* a fast-forward, so no
#: new commit is created and there is nothing to conflict.
DISPOSITION_FAST_FORWARD = "fast_forward"
#: Create a merge commit from objects, with the measured target head as its first parent.
#: Only reachable when the operator turned ``ff_only`` off; a conflict fails closed and is
#: never auto-resolved.
DISPOSITION_MERGE_COMMIT = "merge_commit"

INTEGRATION_DISPOSITIONS: frozenset = frozenset(
    {DISPOSITION_FAST_FORWARD, DISPOSITION_MERGE_COMMIT}
)

# ---------------------------------------------------------------------------
# What the exact-SHA CI gate did.
#
# R2 review j#96350 finding 1 withdrew the `waived` state and the config knob behind it.
# R2 argued from j#96335's configuration list ("branch/target CI を設定駆動") that an optional
# gate was owner-authorized; three durable anchors say otherwise — j#77124 state 5
# ("integrated: origin reachability + exact-SHA CI green を確定"), j#96335's own target flow
# ("exact integration SHA CI green → Close Gate"), and j#96337's fail_closed list ("CI未確定").
# The waiver also had no downstream semantics: the cleanup machine requires a settled green
# CI, so a waived integration either blocked cleanup forever or forced a false self-report.
# The dispute (j#96346) is withdrawn and the gate is mandatory. Only two states remain.
# ---------------------------------------------------------------------------

#: The gate was satisfied by checkable evidence for the exact pushed commit.
CI_GATE_GREEN = "green"
#: The decision never got as far as the CI gate (blocked, terminal no-op, or still pushing).
CI_GATE_NOT_REACHED = "not_reached"

CI_GATE_STATES: frozenset = frozenset({CI_GATE_GREEN, CI_GATE_NOT_REACHED})

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
#: Source-branch CI settled with a non-success conclusion.
BLOCKED_SOURCE_CI_NOT_GREEN = "source_ci_not_green"
#: Source CI evidence was supplied but cannot be checked (missing run / check identity /
#: malformed head). The sibling of the integration-CI token, kept apart for the same reason.
BLOCKED_SOURCE_CI_EVIDENCE_INCOMPLETE = "source_ci_evidence_incomplete"
#: The source CI evidence is about a different commit than the source head being integrated.
BLOCKED_SOURCE_CI_HEAD_MISMATCH = "source_ci_head_mismatch"
#: The configured target ref is not a known, allowlisted integration branch.
BLOCKED_UNKNOWN_TARGET = "unknown_target_branch"
#: The observed target head differs from the expected one recorded in the action: the
#: target advanced since the action was formed. Resolved by re-forming the action against
#: the new head, never by forcing. Checked ONLY before this action has pushed — afterwards
#: the target has moved *because of us*, and comparing it to the pre-push expectation would
#: make every successful integration look like drift (R5 review j#96385 finding 2).
BLOCKED_TARGET_DRIFT = "target_drift"
#: This action's push landed, but the commit it landed is no longer reachable from the
#: target. Something rewrote or removed it after we pushed — a force push, a branch reset, a
#: deleted ref. The post-push counterpart of :data:`BLOCKED_TARGET_DRIFT`, and a different
#: fact: drift means "somebody moved it before us", this means "our own work is gone".
BLOCKED_INTEGRATION_LOST_FROM_TARGET = "integration_lost_from_target"
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
#: The ledger's ``done`` steps are out of dependency order, omit a step a later one depends
#: on, or lack a head a step must carry (R3 review j#96368 finding 3). A push recorded before
#: any apply is the reproduction: the run applied a merge and then reported ``integrated``
#: having pushed nothing, because the map of completed steps never looked at their order.
BLOCKED_LEDGER_UNTRUSTWORTHY = "ledger_untrustworthy"
#: A push was recorded ``done`` but its outcome carries no usable head (R2 review j#96350
#: finding 2). R2 fell back to the source head here, which turned "we failed to record what
#: landed" into "the source landed" — and then matched a merge integration's CI against the
#: wrong commit. There is no fallback: a push that cannot say what it landed has not been
#: shown to have landed anything.
BLOCKED_PUSH_OUTCOME_HEAD_MISSING = "push_outcome_head_missing"
#: The head the push recorded is not the head this disposition should have landed: the source
#: head for a fast-forward, or the apply step's merge commit for a merge disposition.
BLOCKED_PUSH_HEAD_MISMATCH = "push_head_mismatch"
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

#: Precedence for the *primary* reason, most fundamental first. The full set is always
#: reported too, so a durable record shows every failing gate rather than only the first.
_BLOCKED_REASON_PRECEDENCE: Tuple[str, ...] = (
    BLOCKED_ACTION_RECORD_INVALID,
    BLOCKED_MODE_UNRECOGNIZED,
    BLOCKED_ACTION_KEY_MISMATCH,
    BLOCKED_LEDGER_UNTRUSTWORTHY,
    BLOCKED_FOREIGN_WORKTREE,
    BLOCKED_UNKNOWN_TARGET,
    BLOCKED_TARGET_NOT_CONFIGURED,
    BLOCKED_REVIEW_INADMISSIBLE,
    BLOCKED_SOURCE_MUTATED,
    BLOCKED_SOURCE_UNREACHABLE,
    BLOCKED_UNPUSHED_COMMITS,
    BLOCKED_DIRTY_WORKTREE,
    BLOCKED_SOURCE_CI_EVIDENCE_INCOMPLETE,
    BLOCKED_SOURCE_CI_HEAD_MISMATCH,
    BLOCKED_SOURCE_CI_NOT_GREEN,
    BLOCKED_UNRESOLVED_OWNER_GATE,
    BLOCKED_UNRESOLVED_CALLBACK,
    BLOCKED_TARGET_DRIFT,
    BLOCKED_INTEGRATION_LOST_FROM_TARGET,
    BLOCKED_NON_FAST_FORWARD,
    BLOCKED_MERGE_CONFLICT,
    BLOCKED_PUSH_REJECTED,
    BLOCKED_PUSH_OUTCOME_HEAD_MISSING,
    BLOCKED_PUSH_HEAD_MISMATCH,
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

    ``mode`` — :data:`MODE_AUTO` or :data:`MODE_DISABLED`.
    ``integration_branch`` — the configured target ref; ``None`` defers to runtime
    resolution, and a runtime that cannot resolve one fails closed rather than guessing.
    ``ff_only`` — the owner's default (j#96335). ``False`` admits the merge-commit
    disposition; it never admits a rebase or a force push, which have no representation here.

    There are deliberately no CI knobs. R2 shipped ``require_source_ci`` /
    ``require_integration_ci`` and review j#96350 finding 1 ruled them out: j#77124, j#96335's
    target flow, and j#96337's fail-closed list all require green CI for an integration, and a
    config value naming *which* CI to require is not authority to require none. Both gates are
    unconditional, so no field can turn one off.
    """

    mode: str = MODE_DISABLED
    integration_branch: Optional[str] = None
    ff_only: bool = True

    @classmethod
    def default(cls) -> "AutoIntegrationPolicy":
        return cls()

    @property
    def disposition(self) -> str:
        """The disposition this policy admits (never a rebase / force)."""
        return DISPOSITION_FAST_FORWARD if self.ff_only else DISPOSITION_MERGE_COMMIT


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
    decision — :data:`CI_GATE_GREEN` or :data:`CI_GATE_NOT_REACHED`. There is no third
    value: R2's ``waived`` and the config knob behind it were withdrawn by review j#96350
    finding 1 (dispute j#96346 withdrawn in j#96351), so an ``integrated`` decision always
    carries a green exact-SHA gate.
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


def _ci_evidence_problem(
    evidence: Optional[IntegrationCiEvidence],
    *,
    expected_head: str,
    incomplete_reason: str,
    mismatch_reason: str,
    failed_reason: str,
) -> Optional[Tuple[str, str]]:
    """The (token, detail) this CI evidence fails on, or ``None`` if it is green for ``expected_head``.

    One checker for both CI gates so they cannot drift apart — the source gate spent a round
    as a bare boolean while its sibling was typed, which is exactly how two spellings of the
    same rule diverge. ``None`` evidence is NOT a problem here: absence means "not settled
    yet", which each caller renders as its own waiting or blocking state.
    """
    if evidence is None:
        return None
    incomplete = evidence.completeness_errors()
    if incomplete:
        return incomplete_reason, "; ".join(incomplete)
    if evidence.integration_head != expected_head:
        return (
            mismatch_reason,
            f"the CI evidence is about {evidence.integration_head}, not {expected_head}",
        )
    if not evidence.is_green:
        return (
            failed_reason,
            f"required check {evidence.workflow!r} run {evidence.run!r} settled "
            f"{evidence.conclusion!r} on {evidence.integration_head}",
        )
    return None


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
    trusted_recorder: str = "",
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
                "not a Git workspace; there is nothing to integrate "
                "(the separate process retire is unaffected)"
            ),
        )

    # The trusted ledger view is needed before the gates: the target check is a different
    # question before and after this action has pushed (R5 review j#96385 finding 2).
    ledger_entries = tuple(ledger)
    integrity = ledger_integrity_errors(
        ledger_entries,
        action_key=action_key,
        required_order=(STEP_INTEGRATION_APPLY, STEP_PUSH, STEP_INTEGRATION_CI)
        if disposition == DISPOSITION_MERGE_COMMIT
        else (STEP_PUSH, STEP_INTEGRATION_CI),
        head_bearing_steps=(STEP_INTEGRATION_APPLY, STEP_PUSH)
        if disposition == DISPOSITION_MERGE_COMMIT
        else (STEP_PUSH,),
        recorded_by=trusted_recorder,
        known_steps=INTEGRATION_STEPS,
    )
    if integrity:
        return _blocked(
            record,
            tuple(
                BLOCKED_PUSH_OUTCOME_HEAD_MISSING
                if problem == LEDGER_MISSING_HEAD
                else BLOCKED_LEDGER_UNTRUSTWORTHY
                for problem in integrity
            ),
            disposition=disposition,
            action_key=action_key,
            reason="; ".join(integrity),
        )
    done = completed_steps(
        ledger_entries, action_key=action_key, recorded_by=trusted_recorder
    )
    pushed = done.get(STEP_PUSH)

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
    # The source-branch CI gate is unconditional (j#96350 finding 1) and typed against the
    # source head: absent evidence is "not green", and present evidence must be about the very
    # commit being integrated.
    if preflight.source_ci is None:
        blockers.add(BLOCKED_SOURCE_CI_NOT_GREEN)
    else:
        problem = _ci_evidence_problem(
            preflight.source_ci,
            expected_head=record.source_head,
            incomplete_reason=BLOCKED_SOURCE_CI_EVIDENCE_INCOMPLETE,
            mismatch_reason=BLOCKED_SOURCE_CI_HEAD_MISMATCH,
            failed_reason=BLOCKED_SOURCE_CI_NOT_GREEN,
        )
        if problem:
            blockers.add(problem[0])
    if not preflight.owner_gates_resolved:
        blockers.add(BLOCKED_UNRESOLVED_OWNER_GATE)
    if not preflight.callbacks_drained:
        blockers.add(BLOCKED_UNRESOLVED_CALLBACK)
    if pushed is None:
        # Pre-push: the target must still be where the action expected it (CAS).
        if preflight.observed_target_head != record.expected_target_head:
            blockers.add(BLOCKED_TARGET_DRIFT)
    elif not preflight.landed_head_on_target:
        # Post-push: the target has moved BECAUSE OF US, so the question is whether what we
        # landed is still there.
        blockers.add(BLOCKED_INTEGRATION_LOST_FROM_TARGET)
    if blockers:
        return _blocked(record, blockers, disposition=disposition, action_key=action_key)

    # Terminal no-op dispositions. Checked only once every gate is clean, so a blocked
    # action is never reported as already integrated, and checked before any step so the
    # same merge is never re-produced (j#77124 必須訂正2).
    if preflight.already_integrated and pushed is None:
        # Only meaningful before we act. After our own push the source IS reachable from the
        # target by construction, and terminating here would skip the exact-SHA CI gate.
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
    if preflight.patch_equivalent_evidence and pushed is None:
        # R6 review j#96391 finding 4: R6 added this phase fence to `already_integrated` and
        # not to its neighbour, so patch-equivalent evidence supplied after a push short-cut
        # the mandatory exact-SHA CI gate. Both terminals answer "nothing needed doing", which
        # can only be true before we did anything.
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

    # 1. Apply. A fast-forward has nothing to apply, so the step is skipped as
    #    `not_applicable` rather than reported done.
    if disposition == DISPOSITION_MERGE_COMMIT:
        if STEP_INTEGRATION_APPLY not in done:
            # There is no worktree gate here any more, and its absence is the fix rather than
            # an omission. j#77124 forbids the lane's checkout ever holding the target branch;
            # R1 asserted that in a docstring while enforcing nothing (j#96344 finding 3), so
            # R2 made the dedicated checkout's identity a measured, gated fact. Review j#96406
            # finding 1 then showed that gating a *path* cannot work: a foreign lane's
            # checkout swapped onto it between the measurement and the merge was switched off
            # its own branch and had the merge built on it. The merge is now assembled from
            # objects (`merge-tree --write-tree` + `commit-tree`), so no checkout is involved
            # for a rule to protect.
            return IntegrationDecision(
                state=STATE_INTEGRATION_APPLY,
                action_key=action_key,
                next_step=STEP_INTEGRATION_APPLY,
                disposition=disposition,
                reason=(
                    "building the recorded merge disposition as objects onto the measured "
                    "target head (no checkout is switched, and no ref moves until the push)"
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
    # The head the run must be about is the head the push RECORDED landing. R2 fell back to
    # the source head when that record was empty (`... or record.source_head`), which turned
    # "we failed to record what landed" into "the source landed" and let a merge integration
    # be gated by CI about a commit that was never on the target (j#96350 finding 2). There
    # is no fallback, and the recorded head must additionally be the one this disposition
    # should have produced.
    push_outcome = done[STEP_PUSH]
    landed_head = push_outcome.head
    if not is_full_sha(landed_head):
        return _blocked(
            record,
            (BLOCKED_PUSH_OUTCOME_HEAD_MISSING,),
            disposition=disposition,
            action_key=action_key,
            reason=(
                "the push step was recorded done but its outcome carries no full commit "
                "head; a push that cannot say what it landed has not been shown to have "
                "landed anything"
            ),
        )
    expected_landed = (
        done[STEP_INTEGRATION_APPLY].head
        if disposition == DISPOSITION_MERGE_COMMIT
        else record.source_head
    )
    if landed_head != expected_landed:
        return _blocked(
            record,
            (BLOCKED_PUSH_HEAD_MISMATCH,),
            disposition=disposition,
            action_key=action_key,
            reason=(
                f"the push recorded landing {landed_head}, but this {disposition} "
                f"disposition should have landed {expected_landed or 'an applied commit'}"
            ),
        )

    if preflight.integration_ci is None:
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
    problem = _ci_evidence_problem(
        preflight.integration_ci,
        expected_head=landed_head,
        incomplete_reason=BLOCKED_INTEGRATION_CI_EVIDENCE_INCOMPLETE,
        mismatch_reason=BLOCKED_INTEGRATION_CI_HEAD_MISMATCH,
        failed_reason=BLOCKED_INTEGRATION_CI_FAILED,
    )
    if problem:
        return _blocked(
            record,
            (problem[0],),
            disposition=disposition,
            action_key=action_key,
            reason=problem[1],
        )

    return IntegrationDecision(
        state=STATE_INTEGRATED,
        action_key=action_key,
        next_step=None,
        disposition=disposition,
        reason=(
            f"integrated into {record.target_ref} at {landed_head}: origin reachability is "
            "settled and the exact-SHA CI gate is green"
        ),
        integration_ci=CI_GATE_GREEN,
    )


__all__ = (
    "MODE_AUTO",
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
    "BLOCKED_LEDGER_UNTRUSTWORTHY",
    "BLOCKED_MODE_UNRECOGNIZED",
    "BLOCKED_SOURCE_MUTATED",
    "BLOCKED_SOURCE_UNREACHABLE",
    "BLOCKED_REVIEW_INADMISSIBLE",
    "BLOCKED_SOURCE_CI_NOT_GREEN",
    "BLOCKED_UNKNOWN_TARGET",
    "BLOCKED_TARGET_DRIFT",
    "BLOCKED_INTEGRATION_LOST_FROM_TARGET",
    "BLOCKED_NON_FAST_FORWARD",
    "BLOCKED_MERGE_CONFLICT",
    "BLOCKED_DIRTY_WORKTREE",
    "BLOCKED_FOREIGN_WORKTREE",
    "BLOCKED_UNPUSHED_COMMITS",
    "BLOCKED_UNRESOLVED_CALLBACK",
    "BLOCKED_UNRESOLVED_OWNER_GATE",
    "BLOCKED_PUSH_REJECTED",
    "BLOCKED_INTEGRATION_CI_FAILED",
    "BLOCKED_SOURCE_CI_EVIDENCE_INCOMPLETE",
    "BLOCKED_SOURCE_CI_HEAD_MISMATCH",
    "BLOCKED_PUSH_OUTCOME_HEAD_MISSING",
    "BLOCKED_PUSH_HEAD_MISMATCH",
    "BLOCKED_INTEGRATION_CI_EVIDENCE_INCOMPLETE",
    "BLOCKED_INTEGRATION_CI_HEAD_MISMATCH",
    "BLOCKED_TARGET_NOT_CONFIGURED",
    "CI_GATE_GREEN",
    "CI_GATE_NOT_REACHED",
    "CI_GATE_STATES",
    "AutoIntegrationPolicy",
    "IntegrationPreflight",
    "LaneWorktree",
    "checked_merge_status",
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
    "BLOCKED_LEDGER_UNTRUSTWORTHY",
    "StepOutcome",
    "completed_steps",
    "IntegrationActionRecord",
    "build_integration_action_record",
    "CI_CONCLUSION_SUCCESS",
    "IntegrationCiEvidence",
)
