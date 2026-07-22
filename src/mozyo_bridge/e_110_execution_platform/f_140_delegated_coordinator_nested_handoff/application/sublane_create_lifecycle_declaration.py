"""The create path's lane-lifecycle owner declaration (Redmine #13681 W1 / #13647 T1b).

Extracted verbatim from ``HerdrSublaneActuatorOps._declare_lane_lifecycle`` so the
create-time **governance** inputs of a lane's authority row live in one small leaf rather
than inside the 1000-line herdr actuation adapter (module-health leaf extraction — the
allowlisted adapter is at its recorded baseline, so #13647 Tranche 1b's additional
create-time fact could not be threaded through it in place).

The behaviour is unchanged for every pre-#13647 caller: same inputs, same best-effort
contract, same refusal semantics. Tranche 1b adds exactly one optional create-time fact —
``lane_kind``, the delegation-geometry kind (親 / 子 / 孫) the CREATING caller resolved from
durable governance — stored generation-bound on the same authority row so a later heal can
resolve the lane's pane placement OFFLINE (disposition j#85650 P1). Absent (the default) is
the byte-for-byte pre-#13647 declaration.

Boundary: this writes the CAS'd lifecycle authority row. It never writes, reads, or
promotes ``lane_metadata`` / any display projection — those are display joins, never launch
or placement authority (the j#85644 → j#85645 correction).
"""

from __future__ import annotations

import sys
from typing import Optional


def _norm(value: Optional[str]) -> str:
    return (value or "").strip()


def declare_created_lane_lifecycle(
    *,
    repo_workspace_id: str,
    lane_label: str,
    issue: str,
    journal: str,
    worktree_identity: str = "",
    lane_kind: str = "",
) -> None:
    """Declare this lane's owner binding (best-effort, never raises; Redmine #13681 W1).

    A declare needs both the lane unit identity ``(repo_workspace_id, lane_label)`` and the
    durable decision anchor (``--journal``). A create with no journal — or an unresolved
    workspace segment / lane label — is **owner-unbound**: no lifecycle row is written, and
    the lane reads as owner-unbound at the roster and send gate. That is a fail-closed gap
    surfaced honestly downstream, never a guessed owner.

    ``worktree_identity`` (Redmine #13754) is the lane's canonical worktree token, recorded
    here so ``retire --execute`` can prove the caller's ``--worktree`` belongs to this lane.
    It is the SAME token the display-metadata record is keyed on, computed once at the create
    boundary so writer and reader cannot drift.

    ``lane_kind`` (v7, Redmine #13647 Tranche 1b) is the delegation-geometry kind the caller
    resolved from durable governance at THIS create boundary — the one moment the fact is
    available without inference — stored generation-bound as the heal authority for lane-role
    pane placement. Empty means the caller has no durable kind fact: the row records no kind
    and the launch path falls back to ``lane_class`` geometry, byte-for-byte pre-#13647. A
    present non-canonical token is refused by the store before any write; here that refusal
    is caught with the same best-effort contract as every other declare failure — the lane
    stays owner-unbound rather than the actuation breaking.

    The write is best-effort like the metadata upsert: a store error never breaks the
    actuation. A re-run (self-heal, #13378) re-declares and is refused idempotently
    (``already_declared``); a create for an issue another lane still actively owns is refused
    (``owner_conflict``) and the recovery lane stays unbound until an explicit
    ``sublane supersede`` hands ownership over (W2) — both are correct, not errors.
    """
    anchor = _norm(journal)
    issue_id = _norm(issue)
    lane = _norm(lane_label)
    workspace = _norm(repo_workspace_id)
    if not (anchor and issue_id and lane and workspace):
        return
    from mozyo_bridge.core.state.lane_kind import LaneKindError
    from mozyo_bridge.core.state.lane_lifecycle import (
        DecisionPointer,
        DecisionPointerError,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )

    try:
        key = LaneLifecycleKey(workspace, lane)
        decision = DecisionPointer(
            source="redmine", issue_id=issue_id, journal_id=anchor
        )
    except (DecisionPointerError, ValueError):
        # A non-decimal issue / journal cannot anchor a re-readable decision — skip
        # rather than write an owner row no recovery could ever resolve.
        return
    try:
        LaneLifecycleStore().declare_active(
            key,
            decision=decision,
            issue_id=issue_id,
            worktree_identity=worktree_identity,
            # Byte-exact (review j#85852 F1): the store's closed-vocabulary check is the
            # boundary; a padded token is refused there rather than quietly repaired here.
            lane_kind=lane_kind,
        )
    except (LaneLifecycleError, DecisionPointerError, LaneKindError, OSError) as exc:
        print(
            f"warning: lane lifecycle declare skipped ({type(exc).__name__}); "
            "lane reads as owner-unbound",
            file=sys.stderr,
        )


__all__ = ("declare_created_lane_lifecycle",)
