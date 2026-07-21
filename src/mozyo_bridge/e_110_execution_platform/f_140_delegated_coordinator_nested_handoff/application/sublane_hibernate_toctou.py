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

from dataclasses import dataclass, field
from typing import Mapping

# Typed release-boundary block vocabulary (Redmine #13843; split into per-axis subreasons by
# Redmine #14230). Each names the exact fence that fired so the operator sees why the fresh
# re-validation refused the release, distinguishing a worktree content change from a running
# worker turn from a real pending composer input — three genuinely different causes that
# calling for the SAME safe next action (never blind-retry; wait for quiescence / consume the
# real input / do nothing about a worktree race) previously read as one coarse token.
#: A fresh boundary WORKTREE content fingerprint (dirty / untracked / digest) diverged from
#: the preflight capture — a file was modified / added AFTER preflight. Distinct from a
#: running worker turn or a pending composer input (see below): this fires only on a worktree
#: CONTENT change.
BLOCK_WORKTREE_FINGERPRINT_CHANGED = "worktree_fingerprint_changed"
#: A live managed slot is running a worker turn (non-quiescent runtime state) at the boundary
#: — an absolute block regardless of worktree content (Redmine #13843 review F2): a running
#: mutation must never be interrupted by a pane close.
BLOCK_WORKER_BUSY = "worker_busy"
#: A live managed slot carries a REAL pending composer input at the boundary (already
#: ghost-empty-refined upstream, Redmine #14065 — an idle placeholder never reaches this
#: reason). Distinct from :data:`COMPOSER_GHOST_EMPTY_OBSERVED`, which is a safe non-blocking
#: observation, never a block reason.
BLOCK_COMPOSER_PENDING_REAL = "composer_pending_real"
#: Backward-compatibility coarse summary (Redmine #13843's original single token): present
#: whenever any of the three subreasons above fired, so an existing consumer that only checks
#: for this token is unaffected. Redmine #14230: never the ONLY reason in a fresh boundary
#: block — one or more of the three typed subreasons above always accompanies it.
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
#: The boundary WORKTREE fingerprint could not be read (fail closed — never actuate on a
#: worktree we could not prove is unchanged). Distinct from
#: :data:`BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN` below (Redmine #14230): these are two
#: different probes that used to fold into one "unreadable" fact.
BLOCK_WORKTREE_UNREADABLE = "worktree_fingerprint_unreadable"
#: The boundary live RUNTIME/activity probe (worker state / composer read) could not be read,
#: OR returned a successfully-observed-but-unrecognised state (Redmine #13843 review F2's
#: ``unknown`` runtime state — never mistaken for idle). Distinct from
#: :data:`BLOCK_WORKTREE_UNREADABLE`: a worktree fingerprint can be perfectly readable while
#: the runtime/activity probe that observes worker-busy / composer-pending fails or returns
#: an unrecognised state (Redmine #14230).
BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN = "runtime_state_unreadable_or_unknown"

#: Every typed release-boundary block reason this fence can return (Redmine #14230): the
#: closed vocabulary a reader can validate a fresh reason list against. Deliberately excludes
#: :data:`BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT` / :data:`BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT`
#: / :data:`BLOCK_RELEASE_BOUNDARY_ATTESTATION_DRIFT`, which are :func:`revalidate_boundary`'s
#: own separate exact-generation dimensions, not part of THIS module's worktree/activity axis.
RELEASE_BOUNDARY_REASONS: frozenset[str] = frozenset(
    {
        BLOCK_WORKTREE_FINGERPRINT_CHANGED,
        BLOCK_WORKER_BUSY,
        BLOCK_COMPOSER_PENDING_REAL,
        BLOCK_RELEASE_BOUNDARY_MUTATION,
        BLOCK_WORKTREE_UNREADABLE,
        BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN,
    }
)

#: A REAL pending composer input was recognised (never blocks, never sent to the fence) but a
#: distinct ghost-empty placeholder WAS observed at the boundary (Redmine #14065 provider-
#: declared ``dim`` style — a live-admitted idle placeholder, not a genuine unsent prompt).
#: Redmine #14230: surfaced as a safe, secret-free OBSERVATION alongside the block reasons (or
#: an ``ok`` verdict), never itself a block reason — a caller must not treat this as evidence
#: of a real pending input, and must not treat its absence as proof no ghost existed (it is
#: only observed when a live managed slot was actually probed).
COMPOSER_GHOST_EMPTY_OBSERVED = "composer_ghost_empty"

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

# ---------------------------------------------------------------------------
# Reason -> safe next action (Redmine #14230 review j#84793 R1-F2). j#84750 item 3's four
# outcomes, closed and secret-safe: no body / hash / length / substring / path ever appears
# in a next-action token or detail — only the fixed instruction text below.
# ---------------------------------------------------------------------------

#: The evidence is unprovable (a readability failure, not an observed change): re-observe,
#: never guess at what changed.
NEXT_ACTION_READ_RECOVERY = "read_recovery"
#: A live worker is mid-turn: wait for it to reach a quiescent state, never interrupt.
NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION = "wait_for_worker_completion"
#: A REAL pending composer input was observed: it needs an owner-approved disposition
#: (consume or quarantine), never an automatic discard and never a blind retry.
NEXT_ACTION_OWNER_APPROVED_QUARANTINE = "owner_approved_quarantine"
#: A worktree content mutation appeared between preflight and boundary: this specific
#: attempt is refused, but blindly re-issuing the same hibernate risks racing the SAME
#: worker write again — re-observe the lane before retrying.
NEXT_ACTION_NO_BLIND_RETRY = "no_blind_retry"

NEXT_ACTIONS: frozenset[str] = frozenset(
    {
        NEXT_ACTION_READ_RECOVERY,
        NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION,
        NEXT_ACTION_OWNER_APPROVED_QUARANTINE,
        NEXT_ACTION_NO_BLIND_RETRY,
    }
)

_NEXT_ACTION_DETAIL: dict[str, str] = {
    NEXT_ACTION_READ_RECOVERY: (
        "the boundary re-read itself was unreadable (not merely unchanged); re-run the "
        "read (doctor / dry-run preflight) once the transport / runtime state is "
        "observable again — do not infer a mutation from an unreadable probe"
    ),
    NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION: (
        "a live managed slot is mid-turn; wait for it to reach a quiescent runtime state "
        "(awaiting_input / turn_ended), then re-issue hibernate — never interrupt a "
        "running turn with a pane close"
    ),
    NEXT_ACTION_OWNER_APPROVED_QUARANTINE: (
        "a real (non-ghost) pending composer input was observed; it requires an "
        "owner-approved disposition (consume the input, or quarantine the lane) before "
        "hibernate can proceed — never auto-discard it"
    ),
    NEXT_ACTION_NO_BLIND_RETRY: (
        "the worktree content changed between preflight and this boundary re-read; "
        "re-observe the lane (a fresh preflight) before retrying — reissuing the same "
        "hibernate immediately risks racing the same in-flight write again"
    ),
}

#: Decision order when multiple axes fire simultaneously (Redmine #14230 review j#84793
#: R1-F2 "multiple-axis決定順"): the axis whose evidence is least trustworthy / most urgent
#: to resolve wins as the PRIMARY next action. An unreadable probe means nothing else here
#: can even be trusted, so it always wins; a live worker turn must never be interrupted
#: regardless of what else is also true; a real pending composer needs owner attention
#: before anything else; a plain content race is the softest case. Every fired reason's own
#: action is still returned in :func:`release_boundary_next_actions` (see ``actions``) —
#: this order only picks which ONE is ``primary`` for a single-line operator headline.
_NEXT_ACTION_PRIORITY: tuple[str, ...] = (
    NEXT_ACTION_READ_RECOVERY,
    NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION,
    NEXT_ACTION_OWNER_APPROVED_QUARANTINE,
    NEXT_ACTION_NO_BLIND_RETRY,
)

#: The reason -> next-action mapping. Deliberately excludes :data:`BLOCK_RELEASE_BOUNDARY_MUTATION`
#: (a backward-compatibility summary, never actionable on its own) and the exact-generation
#: dimensions (:data:`BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT` / `_REVISION_DRIFT` /
#: `_ATTESTATION_DRIFT`), which are a different axis (#13811) with their own re-verification
#: semantics outside this reason-granularity fix's scope.
_REASON_NEXT_ACTION: dict[str, str] = {
    BLOCK_WORKTREE_UNREADABLE: NEXT_ACTION_READ_RECOVERY,
    BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN: NEXT_ACTION_READ_RECOVERY,
    BLOCK_WORKER_BUSY: NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION,
    BLOCK_COMPOSER_PENDING_REAL: NEXT_ACTION_OWNER_APPROVED_QUARANTINE,
    BLOCK_WORKTREE_FINGERPRINT_CHANGED: NEXT_ACTION_NO_BLIND_RETRY,
}


@dataclass(frozen=True)
class ReleaseBoundaryNextActions:
    """The safe next action(s) for a set of release-boundary reasons (pure, secret-free).

    ``primary`` is the single highest-priority action (:data:`_NEXT_ACTION_PRIORITY`) for a
    one-line operator headline; ``actions`` carries EVERY distinct action implied by the
    fired reasons, in that same priority order (never collapsed to just the primary — a
    caller acting only on ``primary`` while a DIFFERENT axis also fired would silently drop
    an obligation, e.g. clearing a worktree race while a worker is still mid-turn).
    ``details`` maps each action to its fixed, value-free instruction text.
    """

    primary: str = ""
    actions: tuple[str, ...] = ()
    details: Mapping[str, str] = field(default_factory=dict)

    def as_payload(self) -> dict:
        return {
            "primary": self.primary,
            "actions": list(self.actions),
            "details": dict(self.details),
        }


def release_boundary_next_actions(reasons: "tuple[str, ...]") -> ReleaseBoundaryNextActions:
    """Derive the safe next action(s) for a fresh release-boundary reason list (pure).

    Reads only the fixed, closed :data:`_REASON_NEXT_ACTION` mapping — never a value, a
    path, a hash, pane text, or any other secret-shaped input. An empty / all-unmapped
    ``reasons`` (e.g. only :data:`BLOCK_RELEASE_BOUNDARY_MUTATION`, or an exact-generation
    reason outside this axis) yields an empty :class:`ReleaseBoundaryNextActions` — never a
    fabricated action for evidence this function was not given.
    """
    fired = {action for reason in reasons if (action := _REASON_NEXT_ACTION.get(reason))}
    if not fired:
        return ReleaseBoundaryNextActions()
    ordered = tuple(action for action in _NEXT_ACTION_PRIORITY if action in fired)
    return ReleaseBoundaryNextActions(
        primary=ordered[0],
        actions=ordered,
        details={action: _NEXT_ACTION_DETAIL[action] for action in ordered},
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
    fingerprint_worktree_readable: bool,
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

    Redmine #14230: ``fingerprint_boundary.readable`` is the CALLER's fold of two distinct
    probes — the worktree fingerprint and the live activity (worker-busy / composer-pending)
    read (:func:`.sublane_hibernate_boundary.revalidate_boundary`). ``fingerprint_boundary``
    alone cannot tell which one failed, so ``fingerprint_worktree_readable`` is passed
    separately: when the fold is unreadable but the worktree sub-probe was fine, the ACTIVITY
    probe is the one that failed (or returned an unrecognised runtime state) —
    :data:`BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN` rather than
    :data:`BLOCK_WORKTREE_UNREADABLE`. When readable, the three previously-collapsed
    ``diverged_from`` axes are checked and reported separately
    (:data:`BLOCK_WORKTREE_FINGERPRINT_CHANGED` / :data:`BLOCK_WORKER_BUSY` /
    :data:`BLOCK_COMPOSER_PENDING_REAL`); :data:`BLOCK_RELEASE_BOUNDARY_MUTATION` is still
    appended as a backward-compatibility summary whenever any of the three fires, so an
    existing consumer checking only for the coarse token keeps working — it is never the SOLE
    reason in a fresh boundary block.

    Review j#84793 R1-F1 correction: an unreadable PREFLIGHT (T0) worktree capture is ALSO
    :data:`BLOCK_WORKTREE_UNREADABLE`, never :data:`BLOCK_WORKTREE_FINGERPRINT_CHANGED` — an
    unreadable baseline means the content comparison cannot be proven either way (we cannot
    show equivalence, but we equally cannot show a change), which is exactly what
    "unreadable" already means elsewhere in this fence. Folding it into "changed" claimed a
    fact (a mutation happened) the evidence does not support and misdirected recovery toward
    a worktree race instead of a read-recovery next action.
    """
    reasons: list[str] = []
    if not fingerprint_worktree_readable or not fingerprint_preflight.readable:
        reasons.append(BLOCK_WORKTREE_UNREADABLE)
    elif not fingerprint_boundary.readable:
        reasons.append(BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN)
    else:
        subreasons: list[str] = []
        # Absolute-at-boundary axes (Redmine #13843: "regardless of the baseline") — checked
        # first and independently, matching the original diverged_from() precedence.
        if fingerprint_boundary.mutation_in_flight:
            subreasons.append(BLOCK_WORKER_BUSY)
        if fingerprint_boundary.pending_composer:
            subreasons.append(BLOCK_COMPOSER_PENDING_REAL)
        # Content-drift axis. Both captures are already proven readable at this point (the
        # unreadable branch above returns before reaching here), so this is a genuine
        # tuple-mismatch comparison, never a readability fallback.
        content_changed = (
            fingerprint_boundary.dirty,
            fingerprint_boundary.untracked,
            fingerprint_boundary.digest,
        ) != (
            fingerprint_preflight.dirty,
            fingerprint_preflight.untracked,
            fingerprint_preflight.digest,
        )
        if content_changed:
            subreasons.append(BLOCK_WORKTREE_FINGERPRINT_CHANGED)
        if subreasons:
            reasons.extend(subreasons)
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
    "BLOCK_COMPOSER_PENDING_REAL",
    "BLOCK_RELEASE_BOUNDARY_ATTESTATION_DRIFT",
    "BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT",
    "BLOCK_RELEASE_BOUNDARY_MUTATION",
    "BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT",
    "BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN",
    "BLOCK_WORKER_BUSY",
    "BLOCK_WORKTREE_FINGERPRINT_CHANGED",
    "BLOCK_WORKTREE_UNREADABLE",
    "CLEAN_WORKTREE_FINGERPRINT",
    "COMPOSER_GHOST_EMPTY_OBSERVED",
    "NEXT_ACTIONS",
    "NEXT_ACTION_NO_BLIND_RETRY",
    "NEXT_ACTION_OWNER_APPROVED_QUARANTINE",
    "NEXT_ACTION_READ_RECOVERY",
    "NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION",
    "RECOVERY_ACTION_DETAIL",
    "RECOVERY_POST_RELEASE_RESIDUE",
    "RELEASE_BOUNDARY_REASONS",
    "PostReleaseCheck",
    "ReleaseBoundaryNextActions",
    "ReleaseBoundaryRevalidation",
    "WorktreeMutationFingerprint",
    "post_release_check",
    "release_boundary_next_actions",
    "revalidate_release_boundary",
)
