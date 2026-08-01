"""Value objects the #13686 actuator's two state machines share (Redmine #13686).

Extracted from :mod:`...domain.auto_integration_policy` so that module stays within the
module-health line budget once the R1 review (j#96344) required four bare booleans to
become typed, checkable records. The dependency points one way — this module imports
nothing from either policy — so :mod:`...domain.auto_integration_policy` and
:mod:`...domain.retirement_cleanup_policy` both depend on it and not on each other. The
policy module re-exports the public names, mirroring the
``repo_local_config`` / ``repo_local_config_records`` split precedent.

The theme of everything here is the R1 review's central finding: **a boolean cannot be
audited.** ``integration_ci_green: bool`` said a run was green without saying which run,
which check, or which commit — so an unrelated green run satisfied it.
Each is replaced by a record that carries the identity its claim depends on, and each
validates itself against the action it is offered for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

# ---------------------------------------------------------------------------
# Commit identity.
# ---------------------------------------------------------------------------

#: The ``expected_target_head`` sentinel for a target ref that does not exist yet. Spelled
#: explicitly rather than left as an empty string so "the target is empty" is a stated fact
#: and not an omitted field.
EMPTY_TARGET_HEAD = "none"

_HEX_DIGITS = frozenset("0123456789abcdef")


def is_full_sha(value: object) -> bool:
    """True iff ``value`` is exactly 40 lowercase hex digits (a full commit SHA)."""
    if not isinstance(value, str) or len(value) != 40:
        return False
    return all(character in _HEX_DIGITS for character in value)


def normalized_branch(ref: object) -> str:
    """``ref`` as a bare branch name: ``refs/heads/x`` and ``x`` both normalize to ``x``.

    Used wherever two ref spellings must be compared for identity (the configured target vs
    the action's target). Comparing raw strings would let ``refs/heads/main`` and ``main``
    read as different targets and defeat the very check that uses this.
    """
    text = str(ref or "").strip()
    if text.startswith("refs/heads/"):
        text = text[len("refs/heads/") :]
    return text


# ---------------------------------------------------------------------------
# Steps and their outcomes — the "段階別 outcome" the acceptance requires.
# ---------------------------------------------------------------------------

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

#: A record offered as authorization for an action carries a *different* action key. Drift in
#: any identity field produces a new key precisely so a stale record cannot satisfy a new
#: action. Inside the integration decision a mismatched LEDGER entry needs no reason — it is
#: simply not counted, which is the resume contract — so this token belongs to the consumers
#: that must refuse rather than ignore: the post-close cleanup machine, whose destructive
#: steps may only run under the exact integration action that authorized them, and the
#: coordinator confirmation, which authorizes exactly one action.
BLOCKED_ACTION_KEY_MISMATCH = "action_key_mismatch"


# ---------------------------------------------------------------------------
# How an apply ended, as a closed vocabulary rather than a sentence.
# ---------------------------------------------------------------------------

#: The merge succeeded and produced the recorded integration head.
MERGE_MERGED = "merged"
#: The two sides genuinely conflict in content. The only outcome a human has to resolve.
MERGE_CONTENT_CONFLICT = "content_conflict"
#: This git cannot build a merge without a checkout (``merge-tree --write-tree`` needs 2.38),
#: established by reading the version — never inferred from an unrecognized exit code.
MERGE_PRIMITIVE_UNSUPPORTED = "primitive_unsupported"
#: The capability question could not be answered at all: the version command failed or its
#: output was unparseable. R10 review j#96412 required that an unknown operational error not
#: be reported as "unavailable", and R11 collapsed exactly this case into that claim anyway
#: (j#96417 finding 3). Not knowing is its own answer.
MERGE_PROBE_ERROR = "probe_error"
#: The arguments were not what the operation requires: not full SHAs, or a ref name that
#: cannot be turned into a safe refspec.
MERGE_INVALID_INPUT = "invalid_input"
#: The repository's configuration can change what a merge produces — a configured
#: ``merge.<name>.driver`` runs arbitrary code and rewrites the merged content (measured).
#: An actuator that promises the same action rebuilds the same commit cannot keep that
#: promise here, so it refuses rather than producing a commit it cannot reproduce.
MERGE_NONDETERMINISTIC_CONFIG = "nondeterministic_merge_config"
#: ``merge-tree`` failed for an operational reason: a missing or unreadable object, a broken
#: repository. NOT a content conflict, and NOT proof that the primitive is unavailable.
MERGE_ERROR = "merge_error"
#: The merged tree existed but could not be turned into a commit object.
MERGE_COMMIT_ERROR = "commit_error"
#: A port returned something outside this vocabulary. Recorded as a value rather than folded
#: into prose, so a consumer sees "this run produced a status I do not know" instead of a
#: sentence it must parse (j#96417 finding 2).
MERGE_UNRECOGNIZED = "unrecognized_status"

MERGE_STATUSES: frozenset = frozenset(
    {
        MERGE_MERGED,
        MERGE_CONTENT_CONFLICT,
        MERGE_PRIMITIVE_UNSUPPORTED,
        MERGE_PROBE_ERROR,
        MERGE_INVALID_INPUT,
        MERGE_NONDETERMINISTIC_CONFIG,
        MERGE_ERROR,
        MERGE_COMMIT_ERROR,
        MERGE_UNRECOGNIZED,
    }
)


def checked_merge_status(value: object) -> str:
    """Coerce ``value`` to a known merge status, or to :data:`MERGE_UNRECOGNIZED`.

    Fail-closed in the direction that matters: an unknown status must not read as a known
    one, and must not be dropped into prose where only a human could notice it.
    """
    candidate = str(value or "").strip()
    return candidate if candidate in MERGE_STATUSES else MERGE_UNRECOGNIZED


@dataclass(frozen=True)
class StepOutcome:
    """One recorded step outcome, bound to the action key it was performed under.

    ``recorded_by`` is the provenance: which actuator wrote this entry. R3 review j#96368
    finding 3 found the ledger had none, so any caller-authored entry counted as a completed
    step — and a ledger claiming a push that never happened let the run reach ``integrated``
    without ever pushing. An actuator counts only entries it recognises as its own.
    """

    action_key: str
    step: str
    outcome: str
    detail: str = ""
    recorded_by: str = ""
    #: The exact commit the step produced, where it produces one (the integration head an
    #: apply created, or the head a push landed). Empty when the step produces no commit.
    head: str = ""
    #: For an apply: the exact ``git --version`` string the merge ran under. The commit is a
    #: function of the action *given the same git*, and R14 stated that limit in prose while
    #: discarding the only evidence that could check it — the version was read for a
    #: capability comparison and thrown away (j#96435 finding 4). A replay can now be compared
    #: against the version that produced the original.
    git_version: str = ""
    #: For an apply: HOW the merge ended, from the closed :data:`MERGE_STATUSES` vocabulary.
    #: Empty for every other step. R11 shipped the typed status on the port's return value
    #: and then wrote it back into ``detail`` as a string prefix, so the durable record — the
    #: thing a consumer actually reads — was still prose (j#96417 finding 2).
    merge_status: str = ""

    def as_payload(self) -> dict[str, object]:
        """The durable serialization — provenance included.

        R4 review j#96379 finding 1: this dropped ``recorded_by``, so a round trip through
        the durable record silently laundered a foreign entry into an unattributed one. A
        serialization that loses the field the trust decision reads is worse than none.
        """
        return {
            "action_key": self.action_key,
            "step": self.step,
            "outcome": self.outcome,
            "detail": self.detail,
            "head": self.head,
            "recorded_by": self.recorded_by,
            "merge_status": self.merge_status,
            "git_version": self.git_version,
        }

    @classmethod
    def from_payload(cls, payload: "dict[str, object]") -> "StepOutcome":
        """Parse a serialized outcome, keeping every field the trust decision reads."""
        raw_status = payload.get("merge_status", "")
        return cls(
            action_key=str(payload.get("action_key", "")),
            step=str(payload.get("step", "")),
            outcome=str(payload.get("outcome", "")),
            detail=str(payload.get("detail", "")),
            head=str(payload.get("head", "")),
            recorded_by=str(payload.get("recorded_by", "")),
            # A serialized status outside the vocabulary comes back as `unrecognized_status`
            # rather than as itself: a record cannot introduce a new outcome by writing one.
            merge_status=(
                checked_merge_status(raw_status) if str(raw_status or "").strip() else ""
            ),
            git_version=str(payload.get("git_version", "")),
        )


def completed_steps(
    ledger: Iterable[StepOutcome],
    *,
    action_key: str,
    recorded_by: str = "",
) -> dict[str, StepOutcome]:
    """The ``done`` steps recorded under exactly ``action_key`` (later wins).

    Entries under any other key are ignored rather than merged: that is the whole
    idempotency contract. Non-``done`` outcomes are also ignored — a ``blocked`` or
    ``pending`` step has not happened, so it must be evaluated again.

    ``recorded_by``, when given, additionally requires the entry's provenance to match: an
    actuator counts only what it wrote. Omitting it accepts any provenance, which is what a
    direct call to the pure decision does (the actuator always passes its own identity).
    """
    return {
        entry.step: entry
        for entry in ledger
        if entry.action_key == action_key
        and entry.outcome == OUTCOME_DONE
        and (not recorded_by or entry.recorded_by == recorded_by)
    }


#: A ledger whose ``done`` steps are out of dependency order proves nothing about what ran.
LEDGER_ORDER_VIOLATION = "ledger_step_order_violation"
#: A ``done`` step that must name the commit it produced does not.
LEDGER_MISSING_HEAD = "ledger_step_head_missing"
#: The same step is recorded ``done`` more than once for one action. Two records of one
#: event cannot both be the event, and "later wins" would let an appended entry overwrite
#: what actually ran (R4 review j#96379 finding 1).
LEDGER_DUPLICATE_STEP = "ledger_duplicate_step"
#: A ``done`` entry names a step this machine does not have.
LEDGER_UNKNOWN_STEP = "ledger_unknown_step"
#: A ``done`` apply whose recorded merge status is not :data:`MERGE_MERGED`, or any other step
#: carrying a merge status at all. R12 added the status as a durable field and then had no
#: consumer read it, so an apply recorded ``done`` with ``unrecognized_status`` reached
#: ``push_waiting`` and authorized a push (measured, j#96422 finding 2). A field nothing reads
#: is not a gate — the same lesson as the round before, one layer further out.
LEDGER_MERGE_STATUS_UNSOUND = "ledger_merge_status_unsound"


def ledger_integrity_errors(
    ledger: Iterable[StepOutcome],
    *,
    action_key: str,
    required_order: Tuple[str, ...],
    head_bearing_steps: Tuple[str, ...] = (),
    merge_status_step: str = "",
    recorded_by: str = "",
    known_steps: Tuple[str, ...] = (),
) -> Tuple[str, ...]:
    """The reasons this ledger cannot be believed (empty iff it can).

    R3 review j#96368 finding 3: ``completed_steps`` mapped entries without looking at their
    ORDER, so a ledger recording a push before any apply let the run treat the push as done —
    it applied a merge and then reported ``integrated`` having pushed nothing. A recorded step
    is only evidence if the steps it depends on were recorded before it.

    ``required_order`` is the dependency chain (earlier steps must appear earlier in the
    ledger); ``head_bearing_steps`` are the steps whose ``done`` entry must name a full commit
    SHA, because a step that cannot say what it produced has not been shown to have produced
    anything.

    ``merge_status_step`` names the step whose ``done`` entry must ALSO carry
    :data:`MERGE_MERGED`. Anything else — a failure status, an unrecognized one, or none at
    all — means the apply was not shown to have merged, and no step other than that one may
    carry a merge status at all.
    """
    entries = [
        entry
        for entry in ledger
        if entry.action_key == action_key
        and entry.outcome == OUTCOME_DONE
        and (not recorded_by or entry.recorded_by == recorded_by)
    ]
    problems: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.step in seen:
            problems.append(LEDGER_DUPLICATE_STEP)
            break
        seen.add(entry.step)
    permitted = set(known_steps or required_order)
    if any(entry.step not in permitted for entry in entries):
        problems.append(LEDGER_UNKNOWN_STEP)
    positions = {entry.step: index for index, entry in enumerate(entries)}
    ranked = [step for step in required_order if step in positions]
    for earlier, later in zip(ranked, ranked[1:]):
        if positions[earlier] > positions[later]:
            problems.append(LEDGER_ORDER_VIOLATION)
            break
    # A later step recorded without the earlier one it depends on is the same defect: the
    # push-before-apply ledger simply omitted the apply.
    seen_later = False
    for step in reversed(required_order):
        if step in positions:
            seen_later = True
        elif seen_later:
            problems.append(LEDGER_ORDER_VIOLATION)
            break
    for step in head_bearing_steps:
        entry = positions.get(step)
        if entry is not None and not is_full_sha(entries[entry].head):
            problems.append(LEDGER_MISSING_HEAD)
            break
    for entry in entries:
        expected_merged = merge_status_step and entry.step == merge_status_step
        if expected_merged and entry.merge_status != MERGE_MERGED:
            problems.append(LEDGER_MERGE_STATUS_UNSOUND)
            break
        if not expected_merged and entry.merge_status:
            # A status on a step that cannot produce one is a record about something that did
            # not happen; it is refused rather than ignored.
            problems.append(LEDGER_MERGE_STATUS_UNSOUND)
            break
    return tuple(dict.fromkeys(problems))


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
        if not is_full_sha(self.source_head):
            problems.append("source_head must be a full 40-hex commit SHA")
        if self.expected_target_head != EMPTY_TARGET_HEAD and not is_full_sha(
            self.expected_target_head
        ):
            problems.append(
                "expected_target_head must be a full 40-hex commit SHA "
                f"(or {EMPTY_TARGET_HEAD!r} for a target that does not exist yet)"
            )
        return tuple(problems)


def build_integration_action_record(
    *,
    configured_branch: str,
    issue: str,
    lane_generation: int,
    source_head: str,
    expected_target_head: str,
    review_generation: str,
) -> IntegrationActionRecord:
    """Form an action record whose ``target_ref`` comes from the CONFIGURED branch (#13686 R1-F4).

    The one correct way to build a record for a configured actuator. R1 review j#96344
    finding 4 found that the policy's ``integration_branch`` was declared and then read by
    nothing, so a record naming a different target integrated happily — a config field no
    decision reads is not a constraint. The decision now rejects that mismatch, and this
    builder is the other half: a caller that starts from the configured branch cannot
    produce the mismatch in the first place.

    ``configured_branch`` is normalized to a bare branch name, so the record and the policy
    compare equal regardless of which spelling the operator declared.
    """
    return IntegrationActionRecord(
        issue=issue,
        lane_generation=lane_generation,
        source_head=source_head,
        target_ref=normalized_branch(configured_branch),
        expected_target_head=expected_target_head,
        review_generation=review_generation,
    )


# ---------------------------------------------------------------------------
# CI evidence (R1 review j#96344 finding 2).
# ---------------------------------------------------------------------------

#: The only conclusion that is green. Spelled as the literal the CI providers use so the
#: evidence and the run agree on the word.
CI_CONCLUSION_SUCCESS = "success"


@dataclass(frozen=True)
class IntegrationCiEvidence:
    """A CI verdict that can be checked afterwards, bound to the commit it is about.

    Replaces the R1 ``integration_ci_green: bool``. The central preset's
    ``### Hibernate Evidence Marker Contract`` states the rule this implements: evidence
    must be "verdict だけでなく、その verdict を後から検証できる記録" — a run id alone does not
    say *which required check* was green, so an unrelated green run satisfies a bare boolean.
    The same contract's ``required_ci_green`` carries ``workflow`` + ``run`` +
    ``conclusion=success`` and a head envelope; this is that shape.

    ``integration_head`` is the commit the run was *about*. It is compared against the head
    the push actually landed, so a green run on some other commit — the previous integration,
    a sibling branch, the source before the merge — cannot be presented as this action's CI.
    """

    integration_head: str
    workflow: str
    run: str
    conclusion: str

    @property
    def is_green(self) -> bool:
        return self.conclusion == CI_CONCLUSION_SUCCESS

    def completeness_errors(self) -> Tuple[str, ...]:
        """The reasons this evidence cannot be checked (empty iff every field is usable).

        Incompleteness is kept apart from a red conclusion: "we cannot tell what this run
        was" and "the run failed" are different facts, and the operator's next action
        differs (produce a complete record vs fix the build).
        """
        problems: list[str] = []
        if not is_full_sha(self.integration_head):
            problems.append("integration_head must be a full 40-hex commit SHA")
        if not str(self.workflow).strip():
            problems.append("workflow (the required check's identity) is empty")
        if not str(self.run).strip():
            problems.append("run (the run id) is empty")
        if not str(self.conclusion).strip():
            problems.append("conclusion is empty")
        return tuple(problems)

    def as_payload(self) -> dict[str, object]:
        return {
            "integration_head": self.integration_head,
            "workflow": self.workflow,
            "run": self.run,
            "conclusion": self.conclusion,
        }


# ---------------------------------------------------------------------------
# The lane's own checkout (a read-only measurement).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneWorktree:
    """What the actuator measured about the LANE's checkout — the source side, read-only.

    This is the shrunken remnant of ``IntegrationWorktree``, and the shrinking is the point.
    That record described the *dedicated* checkout a merge was applied in, and carried an
    ``admissibility_errors()`` gate because a mutation was about to be performed there.
    Review j#96406 finding 1 showed a gate on a path cannot hold: a foreign lane's checkout
    swapped onto it between the measurement and the merge was switched off its own branch and
    had the merge built on it. The merge is assembled from objects now, so **nothing is ever
    performed in a checkout**, and what is left here describes only the lane whose work is
    being integrated — used to answer "is the source what was reviewed?", never "may I mutate
    this?".

    Every field defaults to its unsatisfied value, so an unreadable checkout blocks.
    """

    path: str
    #: This path is a worktree registered to the repo. An unreadable list reads "no".
    registered: bool = False
    #: No uncommitted / untracked changes: what would be integrated is what was reviewed.
    clean: bool = False
    #: The branch checked out there. Compared against the actuator's own lane branch.
    checked_out_branch: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "registered": self.registered,
            "clean": self.clean,
            "checked_out_branch": self.checked_out_branch,
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
      Note this is a *different* question from whether the ref is the branch this actuator
      was configured for; that one is checked against ``policy.integration_branch``.
    - ``callbacks_drained`` / ``owner_gates_resolved``.

    Typed records (R1 review j#96344 — each replaced a bare boolean that could not be
    audited; all default to ``None``, which is the unsatisfied reading):

    - ``source_ci`` — :class:`IntegrationCiEvidence` for the SOURCE head. R2 left this one a
      bare ``source_ci_green: bool`` while typing its sibling; the same "an unrelated green
      run satisfies it" hole applied, so it carries the same identity.
    - ``integration_ci`` — :class:`IntegrationCiEvidence` for the exact commit the push
      landed, carrying the required check's identity and the run id.
    There is deliberately no ``integration_worktree``. A merge-commit disposition used to be
    applied inside a dedicated checkout, and this carried its measured admissibility; review
    j#96406 finding 1 reproduced a foreign lane's checkout being swapped onto that path
    between the measurement and the merge, so the merge is built from objects now
    (``merge-tree --write-tree`` + ``commit-tree``) and no checkout is involved to describe.
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
    callbacks_drained: bool = False
    owner_gates_resolved: bool = False
    # Typed records (R1 review j#96344, extended R2/R3). `None` is the unsatisfied reading.
    source_ci: Optional[IntegrationCiEvidence] = None
    integration_ci: Optional[IntegrationCiEvidence] = None
    #: Is the commit this action's push landed still reachable from the CURRENT target tip?
    #: Read only once a trusted push receipt exists, and it is what replaces the pre-push
    #: expected-head comparison at that point (R5 review j#96385 finding 2): after our own
    #: push the target has moved by construction, so "it differs from what we expected before
    #: pushing" is not evidence of anything. Defaults to the unsatisfied value.
    landed_head_on_target: bool = False


__all__ = (
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
    "ledger_integrity_errors",
    "LEDGER_ORDER_VIOLATION",
    "LEDGER_MISSING_HEAD",
    "LEDGER_DUPLICATE_STEP",
    "LEDGER_UNKNOWN_STEP",
    "IntegrationActionRecord",
    "build_integration_action_record",
    "CI_CONCLUSION_SUCCESS",
    "IntegrationCiEvidence",
    "LaneWorktree",
    "MERGE_MERGED",
    "MERGE_CONTENT_CONFLICT",
    "MERGE_PRIMITIVE_UNSUPPORTED",
    "MERGE_PROBE_ERROR",
    "MERGE_INVALID_INPUT",
    "MERGE_NONDETERMINISTIC_CONFIG",
    "MERGE_ERROR",
    "MERGE_COMMIT_ERROR",
    "MERGE_UNRECOGNIZED",
    "MERGE_STATUSES",
    "LEDGER_MERGE_STATUS_UNSOUND",
    "checked_merge_status",
    "IntegrationPreflight",
)
