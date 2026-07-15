"""Replacement preservation planner — the pure fail-closed close fence (Redmine #13806).

Tranche A of the atomic self-replacement design (Design Answer j#78384 §3 "Preservation
fence"). A **pure, read-only** decision: given what an action-time adapter *observed*
about a close target versus what the transaction *pinned*, decide whether an additional
process close is safe, and if not, name the closed reason.

Deliberately free of I/O and of the live inventory (``managed-state-model.md``
``### 正本境界``): the planner does not query Herdr, git, or Redmine. The caller (the
tranches B / C actuator) gathers the live facts — a dirty diff, a running mutation, a
missing continuation seal journal, a pending non-replacement approval, an identity /
attestation mismatch — into a :class:`PreservationObservation`, and this module returns a
:class:`PreservationVerdict` from them. Keeping the decision pure lets the fence be pinned
by tests that touch no process and no DB, and keeps *workflow truth* (Redmine) and *live
fact* (the adapter) on the two sides the design draws.

The fence is fail-closed and additive: **any** preservation signal blocks an additional
close. The one carve-out the design fixes (j#78384 §3) is that an already-closed
participant's ``launch_owed`` is a *recovery* step, not a new close — so a caller may keep
launching a fresh slot for a participant whose close already happened even while the fence
would block a *new* close or the self-close. That carve-out lives in the actuator's phase
handling; this planner only ever answers the narrower question "may an additional close
happen now?".
"""

from __future__ import annotations

from dataclasses import dataclass

from mozyo_bridge.core.state.replacement_transaction_model import (
    ParticipantPin,
    norm,
)

# -- closed preservation reason vocabulary (session-boundary.md §1 + j#78384 §3) ---
#
# The session-boundary preservation family (dirty_diff / running_process /
# unrecorded_journal / pending_approval) plus the design's identity / attestation
# fences. A closed vocabulary: an unknown reason is never invented, so a caller can
# switch on it exhaustively.

#: The exact worktree is not clean, or the mutation attribution is not unique.
PRESERVE_DIRTY_DIFF = "dirty_diff"
#: The target is working / mutating, or (for the self participant) its turn has not ended.
PRESERVE_RUNNING_PROCESS = "running_process"
#: The continuation seal journal is missing, Redmine is unreadable, or the expected gate /
#: outbox is unrecorded — the fresh coordinator would have nothing durable to resume from.
PRESERVE_UNRECORDED_JOURNAL = "unrecorded_journal"
#: An owner-action-needed / close approval other than the replacement approval is pending
#: in the participant scope.
PRESERVE_PENDING_APPROVAL = "pending_approval"
#: The observed live slot does not match the pinned identity / evidence (lane / workspace /
#: role / provider / assigned name / locator / revision / cwd / pair / lifecycle revision).
PRESERVE_IDENTITY_MISMATCH = "identity_mismatch"
#: The target's generation-bound startup self-attestation is stale or missing — never
#: adopt, and never close without a positive approval plan (j#78384 §4).
PRESERVE_ATTESTATION_MISSING = "attestation_missing"

PRESERVATION_REASONS = (
    # Ordered most-fundamental first so the verdict lists reasons deterministically.
    PRESERVE_DIRTY_DIFF,
    PRESERVE_RUNNING_PROCESS,
    PRESERVE_UNRECORDED_JOURNAL,
    PRESERVE_PENDING_APPROVAL,
    PRESERVE_IDENTITY_MISMATCH,
    PRESERVE_ATTESTATION_MISSING,
)


@dataclass(frozen=True)
class PreservationObservation:
    """What an action-time adapter observed about a close target (all read-only).

    Each preservation signal is a boolean the caller sets from a live probe; the planner
    never gathers them itself. ``identity_matches`` and ``attestation_fresh`` are the two
    *positive* facts (the slot re-resolved to the pinned identity, and its generation-bound
    attestation is fresh) — when either is ``False`` the corresponding fence fires. They
    default to the safe side: an observation built with no positive evidence blocks
    (``identity_matches=False`` / ``attestation_fresh=False``), so a caller that forgets to
    populate them fails closed rather than open.

    ``detail`` is free-text diagnostic context (never parsed) — e.g. which field of the
    identity diverged — carried onto the verdict for the durable record.
    """

    dirty_diff: bool = False
    running_process: bool = False
    unrecorded_journal: bool = False
    pending_approval: bool = False
    identity_matches: bool = False
    attestation_fresh: bool = False
    detail: str = ""


@dataclass(frozen=True)
class PreservationVerdict:
    """Whether an additional close is safe, and the closed reasons when it is not.

    ``may_close`` is ``True`` only when **no** preservation signal fired (the fence is
    additive and fail-closed). ``reasons`` lists every reason that fired, in
    :data:`PRESERVATION_REASONS` order, so the durable record shows all of them, not just
    the first.
    """

    may_close: bool
    reasons: tuple[str, ...]
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return not self.may_close

    def as_payload(self) -> dict[str, object]:
        return {
            "may_close": self.may_close,
            "reasons": list(self.reasons),
            "detail": self.detail,
        }


def assess_preservation(observation: PreservationObservation) -> PreservationVerdict:
    """Decide whether an additional close is safe from one observation. (pure)

    Fail-closed and additive: any of the six fences blocks. ``identity_matches`` /
    ``attestation_fresh`` are positive facts, so their *absence* fires the identity /
    attestation reason — a missing observation is a block, never a pass.
    """
    reasons: list[str] = []
    if observation.dirty_diff:
        reasons.append(PRESERVE_DIRTY_DIFF)
    if observation.running_process:
        reasons.append(PRESERVE_RUNNING_PROCESS)
    if observation.unrecorded_journal:
        reasons.append(PRESERVE_UNRECORDED_JOURNAL)
    if observation.pending_approval:
        reasons.append(PRESERVE_PENDING_APPROVAL)
    if not observation.identity_matches:
        reasons.append(PRESERVE_IDENTITY_MISMATCH)
    if not observation.attestation_fresh:
        reasons.append(PRESERVE_ATTESTATION_MISSING)
    ordered = tuple(r for r in PRESERVATION_REASONS if r in reasons)
    return PreservationVerdict(
        may_close=not ordered,
        reasons=ordered,
        detail=norm(observation.detail),
    )


def identity_observation_for(
    pin: ParticipantPin,
    *,
    observed_lane_id: str,
    observed_role: str,
    observed_provider: str,
    observed_assigned_name: str,
    observed_locator: str,
    observed_lane_revision: str = "",
    observed_lane_generation: str = "",
) -> bool:
    """Does an observed live slot match a participant pin's identity + evidence? (pure)

    A helper the caller may use to derive :attr:`PreservationObservation.identity_matches`:
    the stable identity ``(lane_id, role, provider, assigned_name)`` and the
    live-generation ``locator`` must all match the pin, and — when the pin carries an issue
    lane's ``(lane_revision, lane_generation)`` immutable pin (scope item 4) — those must
    match too. A pin that carries no lifecycle pin (the default companion / coordinator)
    does not constrain the lifecycle fields. Any divergence returns ``False`` so the
    identity fence fires; this never mutates the #13810 owner row, it only compares.
    """
    if pin.identity != (
        norm(observed_lane_id),
        norm(observed_role),
        norm(observed_provider),
        norm(observed_assigned_name),
    ):
        return False
    if pin.old_locator != norm(observed_locator):
        return False
    if pin.lane_revision and pin.lane_revision != norm(observed_lane_revision):
        return False
    if pin.lane_generation and pin.lane_generation != norm(observed_lane_generation):
        return False
    return True


__all__ = (
    "PRESERVE_DIRTY_DIFF",
    "PRESERVE_RUNNING_PROCESS",
    "PRESERVE_UNRECORDED_JOURNAL",
    "PRESERVE_PENDING_APPROVAL",
    "PRESERVE_IDENTITY_MISMATCH",
    "PRESERVE_ATTESTATION_MISSING",
    "PRESERVATION_REASONS",
    "PreservationObservation",
    "PreservationVerdict",
    "assess_preservation",
    "identity_observation_for",
)
