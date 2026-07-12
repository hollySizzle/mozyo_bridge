"""Config-driven sublane Git worktree / retire-merge policy (Redmine #12604).

The parent (#12603, ``vibes/docs/logics/worktree-lifecycle-boundary.md``) re-evaluates
the #11889 boundary that kept Git worktree lifecycle out of the core: it asks whether a
*config-driven* sublane lifecycle should auto-create a worktree / branch at launch and
attempt a merge to the integration branch before retiring a lane. This module is the
**pure decision core** of that knob — the part with the classical-test and
runtime-authority acceptance — and deliberately holds no IO:

- :func:`decide_worktree_launch` decides the launch *default path* from the resolved
  :class:`SublaneIntegrationPolicy` and a :class:`LaunchPreflight` of runtime facts.
  In a Git workspace with worktree management enabled it creates a worktree / branch;
  in a non-Git directory scaffold it skips (the sublane still runs without a worktree);
  it never clobbers an existing worktree; and it fails closed when the target identity
  is not positively known.
- :func:`decide_retire_integration` decides whether a lane may retire, or records a
  fail-closed :data:`INTEGRATION_BLOCKED` with a concrete reason. This is where the two
  hard acceptance invariants live:

  1. **The runtime preflight is the final authority over the config.** The policy can
     opt *out* of a merge attempt (``merge_on_retire: false``); it can never opt out of
     a safety gate. A dirty worktree, a merge conflict, an unresolved target branch, a
     failed verification, or a missing durable record blocks retirement *whatever the
     config says*.
  2. **The owner-approval / close / callback / durable-anchor invariants cannot be
     disabled by config.** They are required preflight facts here, not config keys (the
     config schema has no field for them), so no ``config.yaml`` can retire a lane whose
     issue is not closed, whose owner approval is missing, whose callbacks are not
     drained, or whose durable record is absent.

Both decisions are pure functions over frozen value objects, mirroring the established
:mod:`...domain.sublane_admission` style (literal machine-readable vocabularies, frozen
inputs / outputs, ``as_payload`` dicts, a journal renderer). All real side effects
(``git worktree add``, ``git merge``, ``git status``) live behind a port in the
application layer; this module only decides and explains.

Scope boundaries (issue #12604 + the worktree-lifecycle boundary doc): it performs no
IO and discovers nothing — every fact is supplied by the caller from the runtime
preflight / durable record; it never removes a worktree, deletes a branch, or touches a
remote (the destructive retirement ops stay coordinator-owned runbook authority); and it
adds no decision authority over any owner / review / close / send gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Launch decision vocabulary (literal; machine-readable regardless of UI language).
# ---------------------------------------------------------------------------

#: Create a worktree / branch — the Git-workspace default path.
LAUNCH_CREATE_WORKTREE = "create_worktree"
#: A worktree already exists for the lane; reuse it rather than clobbering it.
LAUNCH_REUSE_WORKTREE = "reuse_worktree"
#: Not a Git workspace (directory scaffold) — skip worktree creation; the sublane still
#: runs. This is the "Git なし" default path.
LAUNCH_SKIP_NO_GIT = "skip_no_git"
#: Worktree management is disabled by policy (``manage_worktree: false``) — skip.
LAUNCH_SKIP_DISABLED = "skip_disabled"
#: Fail-closed: a Git workspace with management enabled but the target identity /
#: branch is not positively known, so creation against an unverified target is refused.
LAUNCH_BLOCKED = "blocked"

LAUNCH_ACTIONS = frozenset(
    {
        LAUNCH_CREATE_WORKTREE,
        LAUNCH_REUSE_WORKTREE,
        LAUNCH_SKIP_NO_GIT,
        LAUNCH_SKIP_DISABLED,
        LAUNCH_BLOCKED,
    }
)

# ---------------------------------------------------------------------------
# Retire / integration decision vocabulary.
# ---------------------------------------------------------------------------

#: The lane may retire: every safety gate and invariant is satisfied (and the merge, if
#: attempted, succeeded). Retirement itself (pane kill / worktree remove) is the
#: coordinator's separate destructive op; this only authorizes it.
RETIRE_OK = "retire_ok"
#: Fail-closed: retirement is refused; see the recorded reason(s). The lane is *not*
#: retired and the coordinator is called back, per the Sublane Retirement Drain.
INTEGRATION_BLOCKED = "integration_blocked"

RETIRE_STATES = frozenset({RETIRE_OK, INTEGRATION_BLOCKED})

# Concrete ``integration_blocked`` reasons. The first group is the runtime / Git
# preflight (the acceptance's explicit triggers); the second is the config-undisableable
# invariants (owner approval / close / callback / durable anchor).
BLOCKED_PREFLIGHT_FAILURE = "preflight_failure"
BLOCKED_DIRTY_WORKTREE = "dirty_worktree"
BLOCKED_VERIFICATION_FAILURE = "verification_failure"
BLOCKED_TARGET_BRANCH_UNRESOLVED = "target_branch_unresolved"
BLOCKED_MERGE_CONFLICT = "merge_conflict"
BLOCKED_ISSUE_NOT_CLOSED = "issue_not_closed"
BLOCKED_OWNER_APPROVAL_MISSING = "owner_approval_missing"
BLOCKED_UNRESOLVED_CALLBACK = "unresolved_callback"
BLOCKED_DURABLE_RECORD_MISSING = "durable_record_missing"
#: The retire/integration was refused because the latest review generation is not admissible — a
#: stale approval for an older generation, or an unresolved blocking finding in the latest
#: generation (#13518 review R2-F7 / R3-F2). Integration requires the latest generation to be
#: approved AND clean, never merely "an approval exists somewhere". Defined here (above the
#: precedence tuple) so it can rank among the fundamental invariants.
INTEGRATION_STALE_REVIEW_GENERATION = "stale_review_generation"

#: Precedence order for the *primary* blocked reason (most fundamental first): an
#: unidentified target, then the close / owner / callback / durable invariants, then the
#: worktree / verification / merge gates. The full set is always reported too.
_BLOCKED_REASON_PRECEDENCE: Tuple[str, ...] = (
    BLOCKED_PREFLIGHT_FAILURE,
    INTEGRATION_STALE_REVIEW_GENERATION,
    BLOCKED_ISSUE_NOT_CLOSED,
    BLOCKED_OWNER_APPROVAL_MISSING,
    BLOCKED_UNRESOLVED_CALLBACK,
    BLOCKED_DURABLE_RECORD_MISSING,
    BLOCKED_DIRTY_WORKTREE,
    BLOCKED_VERIFICATION_FAILURE,
    BLOCKED_TARGET_BRANCH_UNRESOLVED,
    BLOCKED_MERGE_CONFLICT,
)


# ---------------------------------------------------------------------------
# Resolved policy (the config intent, translated into the domain).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SublaneIntegrationPolicy:
    """The resolved sublane integration policy intent (mirror of the config knob).

    The domain mirror of the ``sublane_integration`` config block so this module
    never imports the governance config schema (the application layer translates
    config -> policy). Every field is *intent*; the preflight below decides what
    actually happens, and the runtime preflight always wins.

    ``manage_worktree`` — create a worktree / branch at launch in a Git workspace.
    ``integration_branch`` — the target branch a retire-time merge integrates into;
    ``None`` defers to runtime resolution (a runtime that cannot resolve fails closed).
    ``merge_on_retire`` — attempt the merge before retiring; ``False`` is the opt-out.
    """

    manage_worktree: bool = True
    integration_branch: Optional[str] = None
    merge_on_retire: bool = True

    @classmethod
    def default(cls) -> "SublaneIntegrationPolicy":
        return cls()


# ---------------------------------------------------------------------------
# Launch: runtime facts + decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchPreflight:
    """Runtime facts for a sublane launch decision (supplied, never discovered here).

    ``is_git_workspace`` distinguishes the Git default path from a directory scaffold.
    ``worktree_exists`` is true when a worktree for this lane / branch already exists
    (reuse, never clobber). ``branch_resolved`` is true once the branch name to create
    is positively determined. ``target_identity_known`` is true once the lane / worktree
    target is positively resolved from the durable record / resolver — a creation
    against an unverified target is refused (fail-closed).
    """

    is_git_workspace: bool
    worktree_exists: bool = False
    branch_resolved: bool = True
    target_identity_known: bool = True


@dataclass(frozen=True)
class WorktreeLaunchDecision:
    """The result of :func:`decide_worktree_launch`."""

    action: str
    reason: str

    @property
    def creates_worktree(self) -> bool:
        return self.action == LAUNCH_CREATE_WORKTREE

    def as_payload(self) -> dict[str, object]:
        return {"action": self.action, "reason": self.reason}


def decide_worktree_launch(
    policy: SublaneIntegrationPolicy, preflight: LaunchPreflight
) -> WorktreeLaunchDecision:
    """Decide the sublane launch default path (pure).

    Precedence:

    1. ``manage_worktree: false`` -> :data:`LAUNCH_SKIP_DISABLED` (operator opt-out).
    2. not a Git workspace -> :data:`LAUNCH_SKIP_NO_GIT` (directory scaffold; the sublane
       runs without a worktree — the "Git なし" path).
    3. target identity / branch not positively known -> :data:`LAUNCH_BLOCKED`
       (fail-closed: never create against an unverified target).
    4. a worktree already exists -> :data:`LAUNCH_REUSE_WORKTREE` (never clobber).
    5. otherwise -> :data:`LAUNCH_CREATE_WORKTREE` (the Git-workspace default path).
    """
    if not policy.manage_worktree:
        return WorktreeLaunchDecision(
            LAUNCH_SKIP_DISABLED,
            "worktree management disabled by policy (manage_worktree: false)",
        )
    if not preflight.is_git_workspace:
        return WorktreeLaunchDecision(
            LAUNCH_SKIP_NO_GIT,
            "not a Git workspace; sublane runs without a worktree (directory scaffold)",
        )
    if not preflight.target_identity_known or not preflight.branch_resolved:
        return WorktreeLaunchDecision(
            LAUNCH_BLOCKED,
            "target lane / branch identity not positively resolved; refusing to "
            "create a worktree against an unverified target",
        )
    if preflight.worktree_exists:
        return WorktreeLaunchDecision(
            LAUNCH_REUSE_WORKTREE,
            "a worktree already exists for this lane; reusing it (never clobbered)",
        )
    return WorktreeLaunchDecision(
        LAUNCH_CREATE_WORKTREE,
        "Git workspace with worktree management enabled; creating worktree / branch",
    )


# ---------------------------------------------------------------------------
# Retire / integration: runtime preflight facts + decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetirePreflight:
    """The runtime preflight facts a retire decision is made from (supplied here).

    These are the *final authority* — the policy never overrides them. They split into
    Git-specific facts (consulted only in a Git workspace) and the
    config-undisableable invariants (always enforced).

    Git-specific (only consulted when ``is_git_workspace`` is true):

    - ``worktree_dirty`` — uncommitted / untracked changes; retiring would discard them.
    - ``integration_branch_resolved`` — the merge target branch is known / exists.
    - ``merge_conflict`` — a merge to the integration branch conflicted.

    Always enforced (the invariants config cannot disable):

    - ``target_identity_known`` — the lane / worktree / pane target is positively
      resolved; a destructive op against an unknown target is refused.
    - ``verification_passed`` — the lane's verification (tests / checks) passed.
    - ``issue_closed`` — the lane's Redmine issue is durably closed (not merely
      ``implementation_done`` / Review-approved).
    - ``owner_approval_present`` — the owner close approval journal exists.
    - ``callbacks_drained`` — no outstanding coordinator callback is owed.
    - ``durable_record_recorded`` — the durable retire record / anchor is present.
    """

    is_git_workspace: bool
    # Git-specific.
    worktree_dirty: bool = False
    integration_branch_resolved: bool = True
    merge_conflict: bool = False
    # Always-enforced invariants.
    target_identity_known: bool = True
    verification_passed: bool = True
    issue_closed: bool = True
    owner_approval_present: bool = True
    callbacks_drained: bool = True
    durable_record_recorded: bool = True
    #: The LATEST review generation is admissible for integration (#13518 review R2-F7 / R3-F2): the
    #: latest generation is approved AND carries no unresolved blocking finding — never merely "an
    #: approval exists somewhere". The field default is the satisfied value so the pure decision and
    #: the config-integration path (which pre-checks it) stay byte-for-byte; the CLI retire path
    #: (:class:`...sublane_lifecycle_command.RetireAssertions`) supplies it FAIL-CLOSED (default
    #: unsatisfied), so the actual `sublane retire` integration can no longer default-admit a stale
    #: last-write-wins approval.
    latest_generation_admissible: bool = True


@dataclass(frozen=True)
class RetireDecision:
    """The result of :func:`decide_retire_integration`.

    ``state`` is :data:`RETIRE_OK` or :data:`INTEGRATION_BLOCKED`. ``blocked_reasons``
    is the full set of failing gates (empty iff ``retire_ok``); ``primary_reason`` is the
    most fundamental one by :data:`_BLOCKED_REASON_PRECEDENCE` (``None`` iff ``retire_ok``).
    ``merge_attempted`` records whether a merge was part of this decision (policy opted in
    *and* it reached the merge stage in a Git workspace); ``merge_performed`` is true only
    on a clean ``retire_ok`` that included a successful merge.
    """

    state: str
    blocked_reasons: Tuple[str, ...] = ()
    primary_reason: Optional[str] = None
    merge_attempted: bool = False
    merge_performed: bool = False

    @property
    def is_blocked(self) -> bool:
        return self.state == INTEGRATION_BLOCKED

    @property
    def may_retire(self) -> bool:
        return self.state == RETIRE_OK

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "blocked_reasons": list(self.blocked_reasons),
            "primary_reason": self.primary_reason,
            "merge_attempted": self.merge_attempted,
            "merge_performed": self.merge_performed,
        }


def decide_retire_integration(
    policy: SublaneIntegrationPolicy, preflight: RetirePreflight
) -> RetireDecision:
    """Decide whether a lane may retire, or fail closed to ``integration_blocked`` (pure).

    Collects every failing gate into ``blocked_reasons`` (so the durable record shows all
    of them, not just the first), then reports the most fundamental as ``primary_reason``.
    Any non-empty set means :data:`INTEGRATION_BLOCKED`.

    Authority rules (the two hard acceptance invariants):

    - The **invariants** (``target_identity_known`` / ``issue_closed`` /
      ``owner_approval_present`` / ``callbacks_drained`` / ``durable_record_recorded`` /
      ``verification_passed``) are checked unconditionally — no policy field can switch
      them off, because the config schema has no key for them.
    - The **merge gate** is the only thing ``merge_on_retire`` controls: ``false`` skips
      the merge requirement (the opt-out), but every other gate still applies, so opting
      out of the merge can never make an unsafe retirement ``ok``.
    - In a **non-Git workspace** the Git-specific gates (dirty worktree, target branch,
      merge) do not apply — a directory-scaffold lane can retire on the invariants alone.
    """
    blockers: set[str] = set()

    # Invariants — always enforced, never disableable by config.
    if not preflight.target_identity_known:
        blockers.add(BLOCKED_PREFLIGHT_FAILURE)
    if not preflight.issue_closed:
        blockers.add(BLOCKED_ISSUE_NOT_CLOSED)
    if not preflight.owner_approval_present:
        blockers.add(BLOCKED_OWNER_APPROVAL_MISSING)
    if not preflight.callbacks_drained:
        blockers.add(BLOCKED_UNRESOLVED_CALLBACK)
    if not preflight.durable_record_recorded:
        blockers.add(BLOCKED_DURABLE_RECORD_MISSING)
    if not preflight.verification_passed:
        blockers.add(BLOCKED_VERIFICATION_FAILURE)
    if not preflight.latest_generation_admissible:
        # #13518 review R2-F7 / R3-F2: the latest review generation is not admissible (a stale
        # approval for an older generation, or an unresolved blocking finding in the latest). The
        # actual integration decision — not only the non-CLI use case — now fences it.
        blockers.add(INTEGRATION_STALE_REVIEW_GENERATION)

    # Git-specific gates — only in a Git workspace.
    merge_attempted = False
    if preflight.is_git_workspace:
        if preflight.worktree_dirty:
            blockers.add(BLOCKED_DIRTY_WORKTREE)
        if policy.merge_on_retire:
            merge_attempted = True
            if not preflight.integration_branch_resolved:
                blockers.add(BLOCKED_TARGET_BRANCH_UNRESOLVED)
            elif preflight.merge_conflict:
                blockers.add(BLOCKED_MERGE_CONFLICT)

    if blockers:
        ordered = tuple(r for r in _BLOCKED_REASON_PRECEDENCE if r in blockers)
        return RetireDecision(
            state=INTEGRATION_BLOCKED,
            blocked_reasons=ordered,
            primary_reason=ordered[0],
            merge_attempted=merge_attempted,
            merge_performed=False,
        )
    return RetireDecision(
        state=RETIRE_OK,
        blocked_reasons=(),
        primary_reason=None,
        merge_attempted=merge_attempted,
        merge_performed=merge_attempted,
    )


# ---------------------------------------------------------------------------
# Durable-record / coordinator-callback renderer.
# ---------------------------------------------------------------------------


def render_integration_decision_journal(
    decision: RetireDecision, *, issue: str, integration_branch: Optional[str] = None
) -> str:
    """Render a retire / integration decision as a durable-record journal (pure).

    On :data:`INTEGRATION_BLOCKED` this is the fail-closed record the lane writes before
    the coordinator callback (Sublane Retirement Drain). On :data:`RETIRE_OK` it is the
    integration-decision record that authorizes the coordinator's destructive retire.
    Only the machine-readable decision fields and the issue id / branch name are emitted
    — never private paths or pane ids (those are added by the coordinator-side retire
    journal).
    """
    heading = (
        "## integration_blocked"
        if decision.is_blocked
        else "## retire integration decision"
    )
    lines = [
        heading,
        "",
        f"- issue: #{issue}",
        f"- state: {decision.state}",
        f"- integration_branch: {integration_branch or 'runtime-resolved'}",
        f"- merge_attempted: {str(decision.merge_attempted).lower()}",
        f"- merge_performed: {str(decision.merge_performed).lower()}",
    ]
    if decision.is_blocked:
        lines.append(f"- primary_reason: {decision.primary_reason}")
        lines.append(
            "- blocked_reasons: " + ", ".join(decision.blocked_reasons)
        )
        lines.append("- next_action: coordinator callback (fail-closed; lane not retired)")
    else:
        lines.append("- blocked_reasons: none")
        lines.append(
            "- next_action: coordinator may proceed to the destructive retire "
            "(pane kill / worktree remove) under the Sublane Retirement Drain preflight"
        )
    return "\n".join(lines)


__all__ = (
    "LAUNCH_CREATE_WORKTREE",
    "LAUNCH_REUSE_WORKTREE",
    "LAUNCH_SKIP_NO_GIT",
    "LAUNCH_SKIP_DISABLED",
    "LAUNCH_BLOCKED",
    "LAUNCH_ACTIONS",
    "RETIRE_OK",
    "INTEGRATION_BLOCKED",
    "INTEGRATION_STALE_REVIEW_GENERATION",
    "RETIRE_STATES",
    "BLOCKED_PREFLIGHT_FAILURE",
    "BLOCKED_DIRTY_WORKTREE",
    "BLOCKED_VERIFICATION_FAILURE",
    "BLOCKED_TARGET_BRANCH_UNRESOLVED",
    "BLOCKED_MERGE_CONFLICT",
    "BLOCKED_ISSUE_NOT_CLOSED",
    "BLOCKED_OWNER_APPROVAL_MISSING",
    "BLOCKED_UNRESOLVED_CALLBACK",
    "BLOCKED_DURABLE_RECORD_MISSING",
    "SublaneIntegrationPolicy",
    "LaunchPreflight",
    "WorktreeLaunchDecision",
    "decide_worktree_launch",
    "RetirePreflight",
    "RetireDecision",
    "decide_retire_integration",
    "render_integration_decision_journal",
)
