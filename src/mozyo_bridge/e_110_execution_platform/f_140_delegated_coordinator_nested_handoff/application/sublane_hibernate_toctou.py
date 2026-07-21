"""`sublane hibernate` release-boundary TOCTOU preservation fence (Redmine #13843).

The pure decision leaf for the hibernate actuation's *action-time* re-validation, split
out of :mod:`sublane_hibernate` so the use-case module stays well under the module-health
ceiling. No IO: the use case injects the live probes (worktree fingerprint + inventory
snapshot) and this module decides whether the release may proceed.

## The gap this closes

Hibernate's fail-closed preflight (:class:`~sublane_hibernate_assertions.HibernateAssertions`
+ the inventory / project-gateway fences) proves the lane is *parked, idle and preservable*
from a durable-record snapshot the operator asserts and a live inventory read taken **once**
at the start of the actuation. But between that preflight snapshot and the managed-process
release (closing the gateway/worker panes), a worker can *start a worktree mutation*: the
``--worktree-clean`` / ``--not-working`` assertions and the idle inventory observation are
NOT atomic with the pane close (Redmine #13843 live observation, #13811 j#79240–j#79256).
A pane closed mid-write leaves an uncommitted, unrecorded ``modified`` / ``untracked``
residue behind, and the row is marked ``hibernated`` while a fresh generation later trips
over ownership-contradicting residue it must reconcile by hand.

## The fence (capture → re-check → actuate → post-check)

The use case takes a :class:`WorktreeMutationFingerprint` at preflight (T0) alongside the
inventory it already reads, and this module supplies two pure gates:

- :func:`revalidate_release_boundary` (T1, **before** the disposition CAS): given a *fresh*
  boundary fingerprint + inventory snapshot, block if the worktree diverged (a new diff /
  untracked file), a mutation is running, a composer prompt is pending, the boundary
  fingerprint is unreadable, or the lane's live managed slot set (assigned-name → locator)
  changed since preflight (a recycled / relaunched / vanished generation). A block here is a
  typed ``blocked`` with **process close 0 / lifecycle transition 0** — nothing is mutated.
- :func:`post_release_check` (T2, **after** the release): given the boundary fingerprint and
  a *post*-release fingerprint, detect an unexpected dirty mutation that raced in during the
  close window. The use case then **withholds success** and converges to the durable
  recovery / boundary-record path (the lane stays ``hibernated`` — issue / worktree / branch
  / commits are preserved, never discarded).

Determinism: the fence has no timing / fault-injection surface. The synthetic TOCTOU
regression drives it by injecting a *scripted* fingerprint / inventory sequence (a boundary
capture that differs from the preflight capture), so a mutation "appearing mid-actuation" is
reproduced deterministically with no sleeps and nothing dangerous enabled in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# Typed release-boundary block vocabulary (Redmine #13843). Each names the exact fence that
# fired so the operator sees why the fresh re-validation refused the release.
#: A fresh boundary fingerprint diverged from the preflight capture, or a mutation / pending
#: composer is live at the boundary: a worktree mutation appeared AFTER preflight.
BLOCK_RELEASE_BOUNDARY_MUTATION = "release_boundary_mutation"
#: The lane's live managed slot set (assigned-name → locator) changed between the preflight
#: snapshot and the boundary re-read, OR the fresh boundary inventory no longer carries the
#: lane's exact declared generation (a recycled / relaunched / provider-rebound / ambiguous
#: process generation the preflight snapshot no longer describes).
BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT = "release_boundary_generation_drift"
#: The lane's lifecycle revision advanced between the preflight read and the boundary re-read
#: (another process — pin repair / replacement / decision update — bumped it), so the release
#: authority the preflight validated is stale (Redmine #13843 review F3; IR j#83536 item 2
#: "lifecycle revision" fresh revalidate).
BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT = "release_boundary_revision_drift"
#: A live managed slot no longer carries an action-time, generation-matched startup
#: attestation at the boundary re-read (Redmine #13843 review F3; IR j#83536 item 2
#: "attestation" fresh revalidate). A missing / stale / conflict / unreadable attestation on
#: any live target fails the boundary closed.
BLOCK_RELEASE_BOUNDARY_ATTESTATION_DRIFT = "release_boundary_attestation_drift"
#: The boundary worktree fingerprint could not be read (fail closed — never actuate on a
#: worktree we could not prove is unchanged).
BLOCK_WORKTREE_UNREADABLE = "worktree_fingerprint_unreadable"

#: The post-release recovery reason: an unexpected dirty mutation was detected AFTER the
#: managed processes were released. Success is withheld and the operator is directed to the
#: durable recovery / boundary-record path.
RECOVERY_POST_RELEASE_RESIDUE = "post_release_dirty_residue"

#: The concrete next action attached to a withheld success (Redmine #13843 acceptance: a
#: durable recovery / boundary-record path with a specific next step). The lane is preserved.
RECOVERY_ACTION_DETAIL = (
    "post-release worktree residue detected: record a boundary journal capturing the "
    "uncommitted diff / resume next-action, then adopt the residue in the lane's next "
    "generation via `sublane resume` (issue / worktree / branch / commits are preserved — "
    "nothing was discarded)"
)


@dataclass(frozen=True)
class WorktreeMutationFingerprint:
    """A cheap, comparable snapshot of a lane worktree's mutation state at one instant.

    Every field defaults to the safe-failing value: an unread fingerprint is
    ``readable=False`` and every derived comparison treats it as *diverged* (fail closed),
    so a probe that could not run never masquerades as "unchanged / clean".

    - :attr:`readable` — the fingerprint was successfully observed. ``False`` folds every
      comparison to *diverged* (never mistaken for "unchanged").
    - :attr:`dirty` / :attr:`untracked` — the worktree carries a tracked-file modification /
      an untracked path. A pre-existing dirty worktree (a dependency park with a boundary
      journal) is legitimate; the fence blocks on a *change*, not on dirtiness itself.
    - :attr:`digest` — a stable content digest over the mutation set (the sorted porcelain
      status lines). It changes when any file's modification / untracked status changes, so a
      mid-actuation worker write flips it even when the coarse ``dirty`` / ``untracked`` flags
      would not.
    - :attr:`mutation_in_flight` / :attr:`pending_composer` — a running worker turn / a
      pending composer input observed live. Either one, present at the boundary, is an
      absolute block (a running mutation must never be interrupted by a pane close).
    """

    readable: bool = False
    dirty: bool = False
    untracked: bool = False
    digest: str = ""
    mutation_in_flight: bool = False
    pending_composer: bool = False

    @property
    def quiescent(self) -> bool:
        """The worktree is readable and shows no live mutation / pending composer."""
        return self.readable and not self.mutation_in_flight and not self.pending_composer

    def diverged_from(self, baseline: "WorktreeMutationFingerprint") -> bool:
        """Did this (later) capture materially change from an earlier ``baseline``?

        Fail-closed: an unreadable capture on EITHER side is treated as diverged (we cannot
        prove equivalence). A live mutation / pending composer observed on THIS capture is
        divergence regardless of the baseline (a running mutation at the boundary always
        blocks). Otherwise divergence is any change in the ``(dirty, untracked, digest)``
        tuple — a new / removed / changed diff or untracked file.
        """
        if not self.readable or not baseline.readable:
            return True
        if self.mutation_in_flight or self.pending_composer:
            return True
        return (self.dirty, self.untracked, self.digest) != (
            baseline.dirty,
            baseline.untracked,
            baseline.digest,
        )


#: The sentinel used when no live worktree probe is wired: a readable, clean, quiescent
#: worktree (a non-git scaffold lane, or an injected fake that does not exercise the fence).
#: Two of these compare equal, so the fence is a no-op for a lane with no VCS mutation
#: surface — matching the pre-#13843 behaviour where such a lane hibernated on assertions
#: alone.
CLEAN_WORKTREE_FINGERPRINT = WorktreeMutationFingerprint(readable=True)


@dataclass(frozen=True)
class ReleaseBoundaryRevalidation:
    """The verdict of the release-boundary (T1) re-validation (Redmine #13843)."""

    ok: bool
    reasons: tuple[str, ...] = ()


def revalidate_release_boundary(
    *,
    fingerprint_preflight: WorktreeMutationFingerprint,
    fingerprint_boundary: WorktreeMutationFingerprint,
    slots_preflight: Mapping[str, tuple[str, str]],
    slots_boundary: Mapping[str, tuple[str, str]],
) -> ReleaseBoundaryRevalidation:
    """Re-validate the worktree + live generation just before the process release (T1).

    Pure and fail-closed. ``slots_*`` are the lane's live managed
    ``{role: (assigned_name, locator)}`` maps (:func:`sublane_process_release.unit_slots`)
    at preflight (T0) and freshly re-read at the boundary (T1). The release may proceed only
    when the fresh boundary fingerprint has NOT diverged from the preflight capture AND the
    live slot set is unchanged. A block returns typed reasons and the caller performs **zero
    lifecycle transition / zero process close** (the disposition CAS has not yet run).
    """
    reasons: list[str] = []
    if not fingerprint_boundary.readable:
        reasons.append(BLOCK_WORKTREE_UNREADABLE)
    elif fingerprint_boundary.diverged_from(fingerprint_preflight):
        reasons.append(BLOCK_RELEASE_BOUNDARY_MUTATION)
    # The live managed slot set changing (a locator recycled to a new pane, a slot relaunched
    # or vanished) is an exact-generation change the preflight snapshot no longer describes —
    # fail closed rather than close a generation we did not re-verify.
    if dict(slots_boundary) != dict(slots_preflight):
        reasons.append(BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT)
    return ReleaseBoundaryRevalidation(ok=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class PostReleaseCheck:
    """The verdict of the post-release (T2) worktree post-check (Redmine #13843)."""

    residue_detected: bool
    reason: str = ""
    recovery_detail: str = ""


def post_release_check(
    *,
    fingerprint_boundary: WorktreeMutationFingerprint,
    fingerprint_post: WorktreeMutationFingerprint,
) -> PostReleaseCheck:
    """Detect an unexpected dirty mutation that raced in during the close window (T2).

    Pure and fail-closed. After the managed processes are released, the worktree fingerprint
    must still match the boundary capture (closing a pane does not touch the worktree). A
    divergence — or an unreadable post fingerprint — means a mutation landed during / after
    the close: success is withheld and the caller converges to the durable recovery /
    boundary-record path. The lane is NOT rolled back (issue / worktree / branch / commits
    are preserved); only the *success report* is withheld.
    """
    if fingerprint_post.diverged_from(fingerprint_boundary):
        return PostReleaseCheck(
            residue_detected=True,
            reason=RECOVERY_POST_RELEASE_RESIDUE,
            recovery_detail=RECOVERY_ACTION_DETAIL,
        )
    return PostReleaseCheck(residue_detected=False)


__all__ = (
    "BLOCK_RELEASE_BOUNDARY_ATTESTATION_DRIFT",
    "BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT",
    "BLOCK_RELEASE_BOUNDARY_MUTATION",
    "BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT",
    "BLOCK_WORKTREE_UNREADABLE",
    "CLEAN_WORKTREE_FINGERPRINT",
    "RECOVERY_ACTION_DETAIL",
    "RECOVERY_POST_RELEASE_RESIDUE",
    "PostReleaseCheck",
    "ReleaseBoundaryRevalidation",
    "WorktreeMutationFingerprint",
    "post_release_check",
    "revalidate_release_boundary",
)
