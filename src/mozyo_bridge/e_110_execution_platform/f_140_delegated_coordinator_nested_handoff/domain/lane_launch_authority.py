"""The lane launch-authority join's typed reason vocabulary (Redmine #14475).

A guarded recovery (``sublane recover-stale`` / ``sublane recover-gateway``) re-joins the
lane's ambient authority immediately before every owed effect: the LIVE lane lifecycle
``(revision, generation)`` must equal the approval's pinned evidence, the lifecycle's
canonical ``worktree_identity`` token must be non-empty AND equal the token freshly derived
from the recovery worktree, and the worktree must resolve to a live checkout on the lane's
expected branch. That join was a bare ``bool``, so the ONLY thing a caller could learn was
"not current" — and the read-only preflight, which had no launch-authority axis at all,
reported ``actionable`` for a lane the launch leg would refuse.

#14462 j#88463 is what that costs: a live ``recover-gateway --execute`` closed the exact old
gateway, then stopped at ``preservation_blocked: launch_authority_moved`` because the lane's
``worktree_identity`` was empty. The destructive leg had already run; the recovery leg never
could. The fence was right — it was just reached one leg too late.

This module is the pure half of the correction: ONE closed vocabulary naming exactly which
axis of the join failed, so

* the SAME evaluator can back both the read-only preflight axis and the action-time launch
  fence (a second implementation is how a preflight drifts away from the effect it predicts),
* a zero-close preflight blocker can name the failing axis and its recovery runbook instead
  of collapsing to an opaque boolean, and
* every token is secret-safe: an axis NAME, never a path, token value, branch, or identity.

Fail-closed in both directions: only the exact :data:`LAUNCH_AUTHORITY_OK` token authorizes a
launch, and any unrecognized token normalizes to :data:`LAUNCH_AUTHORITY_UNKNOWN` — which
does not.
"""

from __future__ import annotations

from mozyo_bridge.core.state.replacement_transaction_model import norm

# -- the closed reason vocabulary ----------------------------------------------

#: Every axis holds: the lane's ambient authority is EXACT and current right now. This is the
#: ONLY token that authorizes an owed launch / send effect.
LAUNCH_AUTHORITY_OK = "ok"

#: The caller supplied no lane lifecycle ``(revision, generation)`` evidence to join against.
#: A destructive refresh is never performed against an unpinned lane, so an unpinned preflight
#: reports this rather than a green axis it cannot actually assert.
LAUNCH_AUTHORITY_PINS_UNPINNED = "lane_pins_unpinned"

#: The lane lifecycle store could not be read (an unreadable / erroring store). Never degraded
#: to "absent" — an unreadable authority is not a proven-missing one.
LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"

#: The lane lifecycle store is readable and positively holds NO row for this lane.
LAUNCH_AUTHORITY_LIFECYCLE_ABSENT = "lifecycle_row_absent"

#: The live lifecycle row exists but its ``(revision, generation)`` no longer equals the
#: approval's pinned evidence — the lane moved under the approval.
LAUNCH_AUTHORITY_GENERATION_MOVED = "lane_revision_generation_moved"

#: The live lifecycle row carries an EMPTY canonical ``worktree_identity`` — the lane was
#: never bound to a worktree (the #14475 producer gap: a supersede-minted recovery row whose
#: later ``sublane create`` declaration was refused ``already_declared`` zero-write). No
#: worktree token can be attested, so no launch may ride this row.
LAUNCH_AUTHORITY_WORKTREE_UNBOUND = "worktree_identity_unbound"

#: The recovery worktree's own canonical token could not be derived (an unreadable /
#: underivable runtime root).
LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE = "worktree_token_underivable"

#: Both tokens are present and they DIFFER — a sibling / wrong / moved worktree.
LAUNCH_AUTHORITY_WORKTREE_MISMATCH = "worktree_identity_mismatch"

#: The recovery worktree does not resolve to a live git checkout.
LAUNCH_AUTHORITY_WORKTREE_UNREADABLE = "worktree_unreadable"

#: The worktree resolves but its current branch is not the lane's expected branch.
LAUNCH_AUTHORITY_BRANCH_DRIFTED = "branch_drifted"

#: The fail-closed default for any token outside this vocabulary. Never authorizes a launch.
LAUNCH_AUTHORITY_UNKNOWN = "unknown"

LAUNCH_AUTHORITY_REASONS = frozenset(
    {
        LAUNCH_AUTHORITY_OK,
        LAUNCH_AUTHORITY_PINS_UNPINNED,
        LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE,
        LAUNCH_AUTHORITY_LIFECYCLE_ABSENT,
        LAUNCH_AUTHORITY_GENERATION_MOVED,
        LAUNCH_AUTHORITY_WORKTREE_UNBOUND,
        LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE,
        LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
        LAUNCH_AUTHORITY_WORKTREE_UNREADABLE,
        LAUNCH_AUTHORITY_BRANCH_DRIFTED,
        LAUNCH_AUTHORITY_UNKNOWN,
    }
)

#: Every token that REFUSES a launch (everything but :data:`LAUNCH_AUTHORITY_OK`).
LAUNCH_AUTHORITY_BLOCKERS = frozenset(LAUNCH_AUTHORITY_REASONS - {LAUNCH_AUTHORITY_OK})


def normalize_launch_authority_reason(token: str) -> str:
    """Normalize a reason token to the closed set. (pure, fail-closed)

    Only an exact member of :data:`LAUNCH_AUTHORITY_REASONS` passes through; anything else —
    empty, free text, a raw store error string — collapses to :data:`LAUNCH_AUTHORITY_UNKNOWN`
    (which refuses). The raw token is never carried onward, so an error string bearing a path
    or a secret can never reach a durable record through this path.
    """
    value = norm(token)
    return value if value in LAUNCH_AUTHORITY_REASONS else LAUNCH_AUTHORITY_UNKNOWN


def launch_authority_current(reason: str) -> bool:
    """Does this reason authorize an owed launch / send effect? (pure, fail-closed)

    The single boolean projection of the join, so the action-time fence and the preflight axis
    can never disagree about what "current" means: ONLY the exact :data:`LAUNCH_AUTHORITY_OK`
    token is current. Every blocker — including :data:`LAUNCH_AUTHORITY_UNKNOWN` — is not.
    """
    return normalize_launch_authority_reason(reason) == LAUNCH_AUTHORITY_OK


#: The operator recovery runbook per blocking axis (secret-safe: it names surfaces and axes,
#: never a path, token value, branch name, or identity).
_RUNBOOKS = {
    LAUNCH_AUTHORITY_OK: "",
    LAUNCH_AUTHORITY_PINS_UNPINNED: (
        "pin the lane lifecycle revision + generation the approval names "
        "(--lane-revision / --lane-generation) and re-run the preflight"
    ),
    LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE: (
        "the lane lifecycle store could not be read; resolve the store / permissions and "
        "re-run the preflight — never actuate against an unreadable authority"
    ),
    LAUNCH_AUTHORITY_LIFECYCLE_ABSENT: (
        "no lifecycle row owns this lane; declare the lane's owner binding "
        "(sublane create / sublane adopt) before any guarded recovery"
    ),
    LAUNCH_AUTHORITY_GENERATION_MOVED: (
        "the lane moved under this approval; re-read the live lifecycle revision + "
        "generation and obtain a fresh owner approval pinned to them"
    ),
    LAUNCH_AUTHORITY_WORKTREE_UNBOUND: (
        "the lane's lifecycle row carries no canonical worktree binding; re-run the lane's "
        "own declaration surface (sublane create / sublane adopt) from the lane worktree so "
        "the bounded binding backfill records the token, then re-run the preflight"
    ),
    LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE: (
        "the recovery worktree's canonical token could not be derived; run from the lane's "
        "own worktree root"
    ),
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH: (
        "this worktree is not the lane's bound worktree; re-run from the exact lane worktree "
        "the lifecycle row is bound to"
    ),
    LAUNCH_AUTHORITY_WORKTREE_UNREADABLE: (
        "the recovery worktree does not resolve to a live git checkout; restore it before any "
        "guarded recovery"
    ),
    LAUNCH_AUTHORITY_BRANCH_DRIFTED: (
        "the worktree is not on the lane's expected branch; restore the lane branch before "
        "any guarded recovery"
    ),
    LAUNCH_AUTHORITY_UNKNOWN: (
        "the lane launch authority could not be classified; re-observe before any guarded "
        "recovery — an unclassifiable authority never authorizes a close"
    ),
}


def launch_authority_runbook(reason: str) -> str:
    """The secret-safe operator recovery hint for a blocking axis. (pure)

    Empty for :data:`LAUNCH_AUTHORITY_OK` (nothing to recover). Any unrecognized token
    normalizes first, so an off-vocabulary reason yields the ``unknown`` runbook rather than
    echoing the caller's raw string back into a durable record.
    """
    return _RUNBOOKS[normalize_launch_authority_reason(reason)]


__all__ = (
    "LAUNCH_AUTHORITY_OK",
    "LAUNCH_AUTHORITY_PINS_UNPINNED",
    "LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE",
    "LAUNCH_AUTHORITY_LIFECYCLE_ABSENT",
    "LAUNCH_AUTHORITY_GENERATION_MOVED",
    "LAUNCH_AUTHORITY_WORKTREE_UNBOUND",
    "LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE",
    "LAUNCH_AUTHORITY_WORKTREE_MISMATCH",
    "LAUNCH_AUTHORITY_WORKTREE_UNREADABLE",
    "LAUNCH_AUTHORITY_BRANCH_DRIFTED",
    "LAUNCH_AUTHORITY_UNKNOWN",
    "LAUNCH_AUTHORITY_REASONS",
    "LAUNCH_AUTHORITY_BLOCKERS",
    "normalize_launch_authority_reason",
    "launch_authority_current",
    "launch_authority_runbook",
)
