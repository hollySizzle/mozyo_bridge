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
which check, or which commit — so an unrelated green run satisfied it. ``coordinator_confirmed:
bool`` said someone approved without saying who, of what, or where it is written down.
``integration_worktree: str`` said "a worktree" without saying it was not the lane's own.
Each is replaced by a record that carries the identity its claim depends on, and each
validates itself against the action it is offered for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

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
# Coordinator confirmation (R1 review j#96344 finding 5).
# ---------------------------------------------------------------------------

#: The only role whose confirmation authorizes an actuation. The actuator performs the
#: coordinator's own operation on their behalf; no other role can hand it that authority.
CONFIRMATION_ISSUER_COORDINATOR = "coordinator"


@dataclass(frozen=True)
class CoordinatorConfirmation:
    """An explicit coordinator confirmation of exactly one action.

    Replaces the R1 ``coordinator_confirmed: bool``, which authorized an actuation on the
    strength of a self-reported flag: it said someone approved without saying who, of what,
    or where it is written down. The central preset's ``### 根拠出所分類`` fixes the general
    rule — "anchor を欠く owner_intent 主張は hearsay として扱う (ラベルではなく anchor が重みを
    決める)" — and a confirmation is exactly such a claim.

    ``action_key`` is the action confirmed, compared for exact equality against the action
    being decided: a confirmation of *some* action is not a confirmation of *this* one, and
    since any identity drift mints a new key, a confirmation cannot survive the world
    changing under it. ``durable_anchor`` is where the confirmation is recorded
    (``#<issue> j#<journal>``), so a later audit can read it rather than take its word.
    """

    action_key: str
    issuer_role: str
    durable_anchor: str

    def admissibility_errors(self, *, action_key: str) -> Tuple[str, ...]:
        """The reasons this confirmation cannot authorize ``action_key`` (empty iff it can)."""
        problems: list[str] = []
        if self.action_key != action_key:
            problems.append(
                "the confirmation names a different action than the one being decided"
            )
        if self.issuer_role != CONFIRMATION_ISSUER_COORDINATOR:
            problems.append(
                f"issuer_role must be {CONFIRMATION_ISSUER_COORDINATOR!r}; got "
                f"{self.issuer_role!r}"
            )
        if not str(self.durable_anchor).strip():
            problems.append(
                "durable_anchor is empty; an unrecorded confirmation is hearsay"
            )
        return tuple(problems)

    def as_payload(self) -> dict[str, object]:
        return {
            "action_key": self.action_key,
            "issuer_role": self.issuer_role,
            "durable_anchor": self.durable_anchor,
        }


# ---------------------------------------------------------------------------
# The dedicated integration worktree (R1 review j#96344 finding 3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationWorktree:
    """The dedicated checkout a merge-commit disposition is applied in.

    Replaces the R1 ``integration_worktree: str``, which was checked only for being
    non-empty and then handed to ``git switch <target>``. j#77124 必須訂正1 requires that
    the lane's own worktree never check out the target branch, and the R1 code *asserted*
    that invariant in a docstring while enforcing nothing — passing the lane's own path made
    the actuator perform the very operation the rule forbids (measured, R1 review finding 3).

    Every field is a measured fact supplied by the caller (the live adapter probes them);
    none is inferred here. All default to the **unsatisfied** value so a caller that omits
    one is refused rather than admitted.
    """

    path: str
    #: This path is a worktree registered to the repo being integrated into — not an
    #: unrelated directory that merely exists.
    registered: bool = False
    #: This path IS the lane's own checkout. The one thing it must not be.
    is_lane_worktree: bool = True
    #: No uncommitted / untracked changes. A merge into a dirty checkout mixes the lane's
    #: leftovers into the integration commit.
    clean: bool = False
    #: The branch currently checked out there, if any. Informational: the adapter switches
    #: to the target itself, so this is for the durable record rather than a gate.
    checked_out_branch: str = ""

    def admissibility_errors(self) -> Tuple[str, ...]:
        """The reasons this worktree may not be integrated in (empty iff it may)."""
        problems: list[str] = []
        if not str(self.path).strip():
            problems.append("path is empty")
        if not self.registered:
            problems.append(
                "the path is not a registered worktree of the target repository"
            )
        if self.is_lane_worktree:
            problems.append(
                "the path is the lane's own worktree; the lane must never check out the "
                "target branch (j#77124)"
            )
        if not self.clean:
            problems.append("the integration worktree is not clean")
        return tuple(problems)

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "registered": self.registered,
            "is_lane_worktree": self.is_lane_worktree,
            "clean": self.clean,
            "checked_out_branch": self.checked_out_branch,
        }


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
    "IntegrationActionRecord",
    "build_integration_action_record",
    "CI_CONCLUSION_SUCCESS",
    "IntegrationCiEvidence",
    "CONFIRMATION_ISSUER_COORDINATOR",
    "CoordinatorConfirmation",
    "IntegrationWorktree",
)
